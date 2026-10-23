"""Canonical filesystem paths.

APFS is case-insensitive by default, so `/Users/x/Chatvault/raw` and
`/Users/x/ChatVault/raw` reach the same folder but are different strings.
Stored as `source_files.root_dir`, they register as two roots. Every path that
is persisted goes through `canonical()` first, so the spelling on disk is the
only spelling in the database.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:  # macOS only; elsewhere realpath is already the on-disk spelling
    import fcntl
    _F_GETPATH = getattr(fcntl, "F_GETPATH", None) if sys.platform == "darwin" else None
except ImportError:  # pragma: no cover
    fcntl = None
    _F_GETPATH = None


def _disk_spelling(existing: str) -> str:
    """The on-disk spelling of a path that exists (macOS F_GETPATH)."""
    if _F_GETPATH is None:
        return existing
    try:
        fd = os.open(existing, os.O_RDONLY)
    except OSError:
        return existing
    try:
        got = fcntl.fcntl(fd, _F_GETPATH, bytes(1024)).split(b"\0", 1)[0].decode()
        return got or existing
    except OSError:
        return existing
    finally:
        os.close(fd)


def canonical(path: str | os.PathLike) -> str:
    """Absolute, symlink-resolved path in the spelling the filesystem stores.

    A path that does not exist (a file that was moved or deleted) is
    canonicalised through its deepest existing ancestor and the remainder is
    appended unchanged, so a stale row's directory part is still normalised."""
    p = os.path.realpath(os.path.expanduser(os.fspath(path)))
    tail: list[str] = []
    probe = p
    while probe and not os.path.exists(probe):
        parent, name = os.path.split(probe)
        if parent == probe:
            break
        tail.append(name)
        probe = parent
    base = _disk_spelling(probe) if os.path.exists(probe) else probe
    return str(Path(base, *reversed(tail))) if tail else base


def is_canonical(path: str | os.PathLike) -> bool:
    return os.fspath(path) == canonical(path)
