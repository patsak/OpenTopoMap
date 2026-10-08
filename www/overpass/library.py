"""The GOL the service answers from, and how fresh it is.

The file is opened on first use, not at import, and opened again whenever it
has been replaced on disk (datasvc rebuilds it after its nightly sync, see
``otmlib.gol``, and swaps it in with a rename). So the service can come up
before its GOL exists - it answers 503 until then - and picks up a rebuild
without a restart.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from geodesk import Features


class LibraryUnavailable(Exception):
    pass


class Library:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._features: Features | None = None
        self._identity: tuple[int, int, int] | None = None

    def features(self) -> Features:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            raise LibraryUnavailable(f"{self.path} does not exist yet; datasvc builds it after its sync") from None
        identity = (st.st_ino, st.st_size, st.st_mtime_ns)
        with self._lock:
            if self._features is None or identity != self._identity:
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
