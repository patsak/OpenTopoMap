"""The GOL the service answers from, and how fresh it is.

The file is opened on first use, not at import, so the service can come up
before its GOL exists - it answers 503 until then.

datasvc replaces the GOL every night it changes (``otmlib.gol``: updated or
rebuilt, then swapped in with a rename), and the service has to follow without
a restart. Opening it again is not enough: geodesk 2.3 keeps every store it has
opened for the life of the process, keyed by the file's real path, so
``Features(path)`` after a swap hands back the old GOL - and keeps the old file
mapped, deleted or not. Only a new process sees the new one. So once the GOL is
replaced, a process that has opened it calls ``on_replaced``; under gunicorn
(overpass.server) that ends the worker after its request, and gunicorn starts
a fresh one in its place. That request is answered from the old GOL.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from geodesk import Features


log = logging.getLogger("overpass")


class LibraryUnavailable(Exception):
    pass


class Library:
    def __init__(self, path: str | os.PathLike, on_replaced: Callable[[Path], None] | None = None):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._features: Features | None = None
        self._identity: tuple[int, int, int] | None = None
        self._on_replaced = on_replaced
        self._replaced_reported = False

    def features(self) -> Features:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            raise LibraryUnavailable(f"{self.path} does not exist yet; datasvc builds it after its sync") from None
        identity = (st.st_ino, st.st_size, st.st_mtime_ns)
        with self._lock:
            if self._features is not None and identity != self._identity:
                # geodesk would hand back the store it already has (see above).
                if not self._replaced_reported:
                    self._replaced_reported = True
                    if self._on_replaced is not None:
                        self._on_replaced(self.path)
                    else:
                        log.warning("%s was replaced; this process keeps the old one until it restarts", self.path)
                return self._features
            if self._features is None:
                features = Features(str(self.path))
                _require_waynode_ids(features, self.path)
                self._features = features
                self._identity = identity
            return self._features

    def timestamp(self) -> str:
        """``timestamp_osm_base``: what otmlib.gol wrote next to the GOL (the
        newest edit in its data), else the time the GOL was written."""
        state = self.path.with_name(self.path.name + ".state.txt")
        try:
            for line in state.read_text(encoding="utf-8").splitlines():
                if line.startswith("timestamp="):
                    return line.split("=", 1)[1].replace("\\:", ":").strip()
        except FileNotFoundError:
            pass
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return ""
        return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_waynode_ids(features: Features, path: Path) -> None:
    """Refuse a GOL built without ``-w``.

    Such a GOL gives every untagged way node the id 0, and that is most nodes:
    ``out`` would print ways whose node lists are all zeros and ``>`` would
    collapse them into a single node. Better no answer than that one.
    """
    way = features("w").first
    if way is None:
        return
    if any(n.id == 0 for n in list(way.nodes)):
        raise LibraryUnavailable(f"{path} was built without waynode IDs; rebuild it with gol build -w")
