"""Explicit secret loading and redaction. Never discover credentials implicitly."""

import os
from pathlib import Path
import re
import stat
import subprocess
from threading import RLock


REDACTED = "[REDACTED]"
TOKEN_PATTERNS = (
    re.compile(r"\bxox[a-z]-[A-Za-z0-9_-]+", re.I),
    re.compile(r"\bya29\.[A-Za-z0-9._~-]+"),
    re.compile(r"\bBearer[ \t]+[A-Za-z0-9._~+/=-]+", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]+"),
)
_loaded_values = set()
_lock = RLock()


class SecretsError(ValueError):
    """Secret loading failed; error messages never include file contents."""


def loaded_secret_values():
    """Internal scanner input; callers must never print these values."""
    with _lock:
        return tuple(_loaded_values)


def redact(text):
    with _lock:
        values = sorted(_loaded_values, key=len, reverse=True)
    for value in values:
        text = text.replace(value, REDACTED)
    for pattern in TOKEN_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def _keychain_read(service, account):
    result = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise SecretsError("Keychain entry could not be loaded")
    return result.stdout.rstrip("\r\n")


class Secrets:
    def __init__(self):
        self._values = {}

    def __repr__(self):
        return "Secrets([REDACTED])"

    def __getitem__(self, name):
        return self._values[name]

    def _store(self, values):
        with _lock:
            self._values.update(values)
            _loaded_values.update(value for value in values.values() if value)

    def load_env(self, path):
        try:
            descriptor = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, encoding="utf-8") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                    raise SecretsError("Secrets file must be a regular file with mode 0600")
                lines = stream.read().splitlines()
        except (OSError, UnicodeError):
            raise SecretsError("Secrets file could not be loaded") from None
        values = {}
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            name, separator, value = line.partition("=")
            name, value = name.strip(), value.strip()
            if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise SecretsError("Invalid secrets env file entry")
            if value.startswith(("'", '"')):
                if len(value) < 2 or value[-1] != value[0]:
                    raise SecretsError("Invalid secrets env file entry")
                value = value[1:-1]
            values[name] = value
        self._store(values)
        return self

    def load_keychain(self, name, *, service, account, reader=None):
        try:
            value = (reader or _keychain_read)(service, account)
            if not isinstance(value, str) or not value:
                raise SecretsError("Empty keychain entry")
        except Exception:
            raise SecretsError("Keychain entry could not be loaded") from None
        self._store({name: value})
        return self
