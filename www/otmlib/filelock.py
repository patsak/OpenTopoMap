"""Locking between processes that fill a shared cache.

A download or conversion that several processes would otherwise start together is
held by one lock file, so the work is done once and the others wait for the result.
"""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def exclusive(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on the lock file *path* until the block exits.

    flock rather than a file whose existence means "locked": the kernel drops the
    lock when the holder dies, so a worker killed mid-download leaves the others
    free instead of wedging them behind a stale marker.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
