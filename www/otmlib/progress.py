"""Throttled progress logging for long build stages."""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

CHECK_EVERY = 2000
INTERVAL_S = 5.0


def _fmt_secs(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"


class Progress:
    """Logs "done/total (pct) rate eta" at most every INTERVAL_S seconds."""

    def __init__(self, label: str, total: int | None = None) -> None:
        self.label = label
        self.total = total if total and total > 0 else None
        self.done = 0
        self._start = time.perf_counter()
        self._last = self._start

    def advance(self, step: int = 1) -> None:
        self.done += step
        if self.done % CHECK_EVERY:
            return
        now = time.perf_counter()
        if now - self._last < INTERVAL_S:
            return
        self._last = now
        elapsed = now - self._start
        rate = self.done / elapsed if elapsed > 0 else 0.0
        if self.total:
            pct = 100.0 * self.done / self.total
            eta = (self.total - self.done) / rate if rate > 0 else 0.0
            log.info(
                "%s: %s/%s (%.0f%%) %.0f/s eta %s",
                self.label,
                self.done,
                self.total,
                pct,
                rate,
                _fmt_secs(eta),
            )
        else:
            log.info("%s: %s done, %.0f/s", self.label, self.done, rate)

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self._start

    def finish(self, detail: str = "") -> None:
        tail = f" {detail}" if detail else ""
        log.info("%s: done %s in %s%s", self.label, self.done, _fmt_secs(self.elapsed), tail)
