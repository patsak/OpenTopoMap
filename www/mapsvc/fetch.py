"""Downloading and unpacking external artifacts.

The transport layer only: *what* is downloaded and where it ends up lives in
[`artifacts.py`](artifacts.py) for the pinned Java tooling and in
[`deps.py`](deps.py) for the sea/bounds data.
"""

from __future__ import annotations

import fnmatch
import hashlib
import zipfile
from pathlib import Path
from typing import Callable, Iterable, Sequence
from urllib.request import Request, urlopen

LogFn = Callable[[str], None]
ProgressFn = Callable[[str, int, int | None], None]

USER_AGENT = "OpenTopoMap-garmin-server/1.0"
CHUNK_BYTES = 1024 * 1024
TIMEOUT_S = 600


def download_percent(done: int, total: int | None) -> int | None:
    if total is None or total <= 0 or done < 0:
        return None
    return min(100, int(done * 100 / total))


def human_bytes(n: int) -> str:
    size = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)}B" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}GB"


class ConsoleProgress:
    """Print download progress to a log function, throttled to ~5% steps."""

    def __init__(self, log: LogFn) -> None:
        self._log = log
        self._label = ""
        self._last_pct = -1

    def __call__(self, label: str, done: int, total: int | None) -> None:
        if label != self._label:
            self._label = label
            self._last_pct = -1
        pct = download_percent(done, total)
        if pct is None:
            if done == 0:
                self._log(label)
            elif done > 0:
                self._log(f"{label}: {human_bytes(done)}")
            return
        if self._last_pct >= 0 and pct < 100 and pct < self._last_pct + 5:
            return
        self._last_pct = pct
        extra = f" ({human_bytes(done)} / {human_bytes(total)})" if total else ""
        self._log(f"{label}: {pct}%{extra}")


def download(
    url: str,
    dest: Path,
    log: LogFn,
    progress: ProgressFn | None = None,
    label: str = "",
    sha256: str | None = None,
) -> None:
    """Fetch ``url`` into ``dest``, checking ``sha256`` before the file appears.

    The body goes to a ``.part`` file that is only renamed once the digest
    matches, so an interrupted or tampered download can never be mistaken for an
    installed artifact.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.unlink(missing_ok=True)
    log(f"Downloading {url}")
    title = label or dest.name
    reporter = progress if progress is not None else ConsoleProgress(log)
    digest = hashlib.sha256()
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=TIMEOUT_S) as resp, tmp.open("wb") as out:
        total_header = resp.headers.get("Content-Length")
        try:
            total = int(total_header) if total_header else None
        except ValueError:
            total = None
        if total is not None and total <= 0:
            total = None
        done = 0
        if reporter is not None:
            reporter(title, 0, total)
        while True:
            chunk = resp.read(CHUNK_BYTES)
            if not chunk:
                break
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if reporter is not None:
                reporter(title, done, total)
    if sha256 is not None and digest.hexdigest() != sha256:
        actual = digest.hexdigest()
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{url}: sha256 mismatch\n  expected {sha256}\n  got      {actual}")
    tmp.replace(dest)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _member_target(name: str, strip_top: bool) -> str | None:
    """Path of a zip entry relative to ``dest_dir``, or ``None`` to skip it.

    Rejects the absolute and ``..`` entries a zip is free to contain: nothing
    here is unpacked outside the directory the caller named.
    """
    if name.endswith("/"):
        return None
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if strip_top:
        parts = parts[1:]
    if not parts or any(p == ".." for p in parts) or name.startswith("/"):
        return None
    return "/".join(parts)


def extract(
    zip_path: Path,
    dest_dir: Path,
    members: Sequence[str] | None = None,
    strip_top: bool = False,
) -> list[str]:
    """Unpack ``zip_path`` into ``dest_dir``, returning the paths written.

    ``members`` are glob patterns matched against the path *inside* the archive
    (after ``strip_top`` drops the distribution's own top-level directory); with
    no patterns the whole archive is unpacked. Extracting a subset is the point:
    a mkgmap distribution carries a doc/ and examples/ tree — and, historically,
    a Finnix torrent — that nothing in the build ever reads.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            rel = _member_target(info.filename, strip_top)
            if rel is None:
                continue
            if members is not None and not _matches(rel, members):
                continue
            target = dest_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                while True:
                    chunk = src.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    out.write(chunk)
            written.append(rel)
    return written


def _matches(rel: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(rel, pattern) for pattern in patterns)
