"""Add-only path denials, checked before file access."""

import logging
import os
from pathlib import Path


logger = logging.getLogger(__name__)
DEFAULTS = {"extra_denied_paths": []}


class PathDeniedError(PermissionError):
    """A requested path is denied or cannot safely be resolved."""


class PathGuard:
    def __init__(self, extra_denied_paths=(), *, home=None):
        self.home = Path(home if home is not None else Path.home()).absolute()
        self._denied = set()
        self.add(self.home / ".hermes")
        for path in extra_denied_paths:
            self.add(path)

    def _absolute(self, path):
        text = os.fspath(path)
        if text == "~" or text.startswith("~/"):
            path = self.home / text[2:] if text != "~" else self.home
        elif text.startswith("~"):
            raise PathDeniedError("Named-user home expansion is not allowed")
        # Preserve '..' until symlinks have been resolved: collapsing it first
        # can hide a symlink that leads into a denied parent directory.
        return Path(path).absolute()

    @property
    def denied_paths(self):
        return tuple(sorted(self._denied))

    def add(self, path):
        absolute = self._absolute(path)
        self._denied.update((absolute, absolute.resolve()))

    def remove(self, path):
        logger.warning("path_denied: denied paths are add-only")
        raise PathDeniedError("Denied paths cannot be removed")

    def check(self, path):
        try:
            absolute = self._absolute(path)
            # Check the lexical path first, before resolving inside a denied tree.
            if any(absolute.is_relative_to(denied) for denied in self._denied):
                raise PathDeniedError("Path is denied")
            resolved = absolute.resolve()
            if any(resolved.is_relative_to(denied) for denied in self._denied):
                raise PathDeniedError("Path is denied")
            # Python versions differ on whether non-strict resolve raises for
            # symlink loops. stat catches ELOOP while permitting new files.
            try:
                resolved.stat()
            except FileNotFoundError:
                pass
            return resolved
        except (OSError, RuntimeError, PathDeniedError):
            logger.warning("path_denied: file access refused")
            raise PathDeniedError("Path is denied or cannot safely be resolved") from None
