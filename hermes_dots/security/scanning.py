"""Git-scoped scanners; findings contain locations, never source lines."""

from pathlib import Path
import re
import subprocess

from .secrets import TOKEN_PATTERNS, loaded_secret_values, redact


def repo_files(root):
    root = Path(root).resolve()
    result = subprocess.run(["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
                            capture_output=True, check=False)
    if result.returncode:
        raise ValueError("Scanner root must be a Git working tree")
    for name in sorted(set(result.stdout.split(b"\0")) - {b""}):
        path = root / name.decode("utf-8", errors="surrogateescape")
        # Never follow a repo symlink into a private or external file.
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Scanner refuses symlink or escaped file")
        if path.is_file():
            yield path


def credential_hits(root):
    values = loaded_secret_values()
    for path in repo_files(root):
        for number, line in enumerate(path.read_bytes().decode("utf-8", errors="replace").splitlines(), 1):
            if any(pattern.search(line) for pattern in TOKEN_PATTERNS) or any(value in line for value in values):
                yield f"{redact(str(path.relative_to(root)))}:{number}: credential [REDACTED]"


def mask_term(term):
    # A short term must also never be printed in full.
    visible = min(2, max(0, len(term) - 1))
    return term[:visible] + "*" * (len(term) - visible)


def personal_hits(root, terms):
    patterns = [(re.compile(re.escape(term), re.I), mask_term(term)) for term in terms if term]
    for path in repo_files(root):
        location = str(path.relative_to(root))
        for pattern, masked in patterns:
            location = pattern.sub(lambda match, masked=masked: masked, location)
        for number, line in enumerate(path.read_bytes().decode("utf-8", errors="replace").splitlines(), 1):
            for pattern, masked in patterns:
                if pattern.search(line):
                    yield f"{redact(location)}:{number}: personal term {masked}"
