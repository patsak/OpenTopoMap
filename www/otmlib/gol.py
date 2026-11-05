"""The GeoDesk GOL www/overpass answers from, built out of the region PBFs.

datasvc keeps it current as the last step of its nightly pass, right after the
sync (datasvc.job.build_gol), so it follows the extracts without anything
watching them. By hand, from www/:

    python -m otmlib.gol mapsvc/data/gol/armenia.gol \\
        mapsvc/data/geofabrik-cache/armenia-latest.osm.pbf

Several regions become one GOL, not one per region. A query then sees one
dataset - a relation or a way crossing a district border is one element, as
in Overpass, instead of two clipped halves in two files. They go through
``osmium merge`` and then ``osmium time-filter``: merge drops an object
repeated across extracts only when it is the same version in each, so a
region that missed a night's diffs would otherwise put two versions of
everything along its border into the GOL. time-filter keeps the newest.

Two ways to bring it up to date:

* :func:`build` - from the PBFs, from scratch. Tens of minutes for Russia, and
  many times the PBFs' size in scratch space.
* :func:`update` - ``gol update`` (gol 2.5+) with the regions' own ``.osc.gz``
  diffs, the same ones the sync applied to the PBFs. Seconds. The diffs of all
  regions go in as one change file made by ``osmium merge-changes -s``, which
  keeps the newest version of an object that several extracts' diffs carry -
  the counterpart of time-filter in a build. Checked against a build on
  Kazakhstan + Kyrgyzstan over 13 days: the same features, element for
  element, except that gol 2.5.3 reads ``&#xA;`` in a tag value as nothing,
  so a value with a newline in it loses the newline until the next build.

gol locks a GOL it updates against every reader, and the service holds this
one open, so the update runs on a copy that is then renamed over the GOL, as a
build is. An updated GOL is sparse - gol leaves holes where it moved tiles -
so it is copied hole for hole (:func:`_copy_sparse`); a plain copy would
double its size on disk.

The GOL is renamed over the previous one only once it is complete, so the
service never opens a half-written file - it sees the new one on its next
query (overpass.library). Both are done in ``OTM_GOL_WORK_DIR`` when that is
set, else next to the GOL. The work dir exists for data dirs gol cannot build
on: Colima on macOS mounts the host's directories into its VM over sshfs,
which refuses an operation gol needs ("Operation not supported"), so there the
GOL is built on the container's own disk and copied over.

Next to the GOL goes ``<name>.gol.state.txt``:

* ``timestamp=`` - Overpass's ``timestamp_osm_base``, the newest edit in each
  PBF and the oldest of those across them, since the whole is only as fresh
  as its stalest part. Read out of the objects by osmium: datasvc applies the
  diffs in place and keeps the sequence in Postgres, so the PBF header and
  Geofabrik's state.txt beside it still describe the first download.
* ``source=`` - name, size and mtime of each PBF, which is how
  ``is_current`` knows a GOL was built from exactly these files.
* ``sequence=`` - for each PBF, the replication sequence the GOL holds it at,
  which is where the next :func:`update` starts. Absent when one is unknown:
  such a GOL can only be rebuilt.
* ``newest=`` - each PBF's newest edit, which ``timestamp=`` is the oldest
  of, carried forward by updates.
* ``built=`` - when the last full build finished; updates leave it alone.

``-w`` (waynode IDs) is not optional: without it every untagged way node has
id 0, the service refuses the GOL, and gol refuses to update it.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Free space a build needs in its work dir, as a multiple of the input PBFs'
# total size: the merged PBF, gol's working files and the GOL it writes, at
# their peak. Measured on the nine Russian federal districts: 4.2 GB of PBF,
# 27.7 GB at the peak (6.6x) - gol's sort files dwarf the 6.6 GB GOL it ends
# with. 8x leaves a margin.
WORK_SPACE_FACTOR = 8.0

# Free space an update needs, as a multiple of the GOL's size on disk: the
# copy it works on, and what that grows by. 13 days of Kazakhstan +
# Kyrgyzstan grew 532 MB on disk by 8 MB.
UPDATE_SPACE_FACTOR = 1.5


def gol_binary() -> str:
    return os.environ.get("OTM_GOL_BIN", "gol")


def osmium_binary() -> str:
    return os.environ.get("OTM_OSMIUM_BIN", "osmium")


class GolBuildError(RuntimeError):
    pass


@dataclass
class State:
    """What ``<name>.gol.state.txt`` says, keyed by PBF file name."""

    timestamp: str | None = None
    sources: list[str] = field(default_factory=list)
    sequences: dict[str, int] = field(default_factory=dict)
    newest: dict[str, str] = field(default_factory=dict)
    built: str | None = None

    def lines(self) -> list[str]:
        out = []
        if self.timestamp:
            out.append(f"timestamp={self.timestamp}")
        if self.built:
            out.append(f"built={self.built}")
        out += [f"source={s}" for s in self.sources]
        out += [f"sequence={name} {seq}" for name, seq in self.sequences.items()]
        out += [f"newest={name} {ts}" for name, ts in self.newest.items()]
        return out


def build(
    pbfs: list[Path],
    target: Path,
    *,
    sequences: dict[str, int] | None = None,
    expected_sources: list[str] | None = None,
) -> None:
    """Build *target* from *pbfs*, from scratch.

    *sequences* (PBF name -> replication sequence) is recorded for the
    updates that follow. It is only true of the files as they were when the
    caller read it, which *expected_sources* (their :func:`fingerprint` then)
    says: a PBF the sync rewrote in between would put diffs into the GOL that
    the next update would apply again, so then no sequence is recorded and
    the next pass builds instead.
    """
    missing = [p for p in pbfs if not p.is_file()]
    if missing:
        raise FileNotFoundError("missing: " + ", ".join(str(p) for p in missing))
    # Taken before reading, so a PBF rewritten meanwhile reads as changed.
    sources = fingerprint(pbfs)
    if sequences is not None and expected_sources is not None and sources != expected_sources:
        log.warning("GOL: an extract changed since its sequence was read; the next pass rebuilds")
        sequences = None
    target.parent.mkdir(parents=True, exist_ok=True)
    work_root = work_dir() or target.parent
    work_root.mkdir(parents=True, exist_ok=True)
    _require_space(int(sum(p.stat().st_size for p in pbfs) * WORK_SPACE_FACTOR), work_root, "building a GOL")
    with tempfile.TemporaryDirectory(dir=work_root, prefix=".building-") as work:
        source = pbfs[0]
        if len(pbfs) > 1:
            source = Path(work) / "merged.osm.pbf"
            log.info("GOL: merging %d extracts", len(pbfs))
            _merge(pbfs, source)
        # gol appends ".gol" to the name it is given.
        scratch = Path(work) / "library"
        log.info("GOL: building %s", target)
        _run_gol(["build", str(scratch), str(source), "-w"], work_root)
        _move_into_place(scratch.with_suffix(".gol"), target)
    newest = {p.name: newest_object(p) for p in pbfs}
    state = State(
        sources=sources,
        sequences=dict(sequences or {}),
        newest={name: ts for name, ts in newest.items() if ts is not None},
        built=_now(),
    )
    # Unknown for one part means unknown for the whole: overpass.library then
    # falls back to the GOL's mtime.
    if all(ts is not None for ts in newest.values()):
        # ISO-8601 in UTC with a Z compares correctly as text.
        state.timestamp = min(newest.values())
    _write_state(target, state)
    log.info("GOL: %s is ready", target)


def update(
    target: Path,
    changes: dict[str, tuple[int, list[Path]]],
    *,
    sources: list[str] | None = None,
) -> None:
    """Apply the regions' diffs to *target* with ``gol update``.

    *changes* maps a PBF name, as the state file knows it, to the sequence its
    diffs bring it to and those diffs, oldest first. PBFs left out stay where
    they are. *sources* replaces the recorded fingerprints - the PBFs the GOL
    now matches.
    """
    state = read_state(target)
    if state is None or not target.is_file():
        raise GolBuildError(f"{target} has no state to update from; build it")
    unknown = sorted(set(changes) - set(state.sequences))
    if unknown:
        raise GolBuildError(f"{target} holds no sequence for {', '.join(unknown)}; build it")
    oscs = [osc for _, files in changes.values() for osc in files]
    work_root = work_dir() or target.parent
    work_root.mkdir(parents=True, exist_ok=True)
    _require_space(int(_disk_size(target) * UPDATE_SPACE_FACTOR), work_root, "updating the GOL")
    with tempfile.TemporaryDirectory(dir=work_root, prefix=".updating-") as work:
        copy = Path(work) / "library.gol"
        log.info("GOL: updating a copy of %s with %d diff(s)", target.name, len(oscs))
        _copy_sparse(target, copy)
        if oscs:
            merged = Path(work) / "changes.osc.gz"
            subprocess.run(
                [osmium_binary(), "merge-changes", "-s", *map(str, oscs), "-o", str(merged), "--overwrite"],
                check=True,
            )
            _run_gol(["update", str(copy), str(merged)], work_root)
        _move_into_place(copy, target)
    for name, (sequence, files) in changes.items():
        state.sequences[name] = sequence
        stamps = [s for s in (newest_object(f) for f in files) if s is not None]
        if name in state.newest:
            stamps.append(state.newest[name])
        if stamps:
            state.newest[name] = max(stamps)
    if sources is not None:
        state.sources = sources
    if state.newest and set(state.newest) == set(state.sequences):
        state.timestamp = min(state.newest.values())
    _write_state(target, state)
    log.info("GOL: %s is updated", target)


def _require_space(need: int, work_root: Path, what: str) -> None:
    """Refuse up front rather than let gol run out of disk half-way.

    gol maps its working files into memory, so a full disk does not reach it
    as ENOSPC: the kernel kills it with SIGBUS, hours into a build on a slow
    machine, with nothing to say why.
    """
    free = shutil.disk_usage(work_root).free
    if free < need:
        raise GolBuildError(f"{what} needs about {_gb(need)} free in {work_root}; there is {_gb(free)}")


def _run_gol(args: list[str], work_root: Path) -> None:
    try:
        subprocess.run([gol_binary(), *args], check=True)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == -signal.SIGBUS:
            raise GolBuildError(
                f"gol died with SIGBUS: {work_root} most likely ran out of space "
                f"(there is {_gb(shutil.disk_usage(work_root).free)} free now that its files are gone)"
            ) from exc
        raise


def _gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _disk_size(path: Path) -> int:
    """Bytes *path* takes on disk - less than its length when it is sparse."""
    st = path.stat()
    blocks = getattr(st, "st_blocks", None)
    return st.st_size if blocks is None else min(st.st_size, blocks * 512)


def work_dir() -> Path | None:
    configured = os.environ.get("OTM_GOL_WORK_DIR", "").strip()
    return Path(configured) if configured else None


# Runs of zeros at least this long become holes in a copy.
_SPARSE_CHUNK = 1 << 20


def _copy_sparse(src: Path, dst: Path) -> None:
    """Copy *src* to *dst*, leaving holes where *src* reads as zeros.

    By content rather than by SEEK_HOLE, which neither every filesystem nor
    every sshfs answers truthfully; reading a hole costs no disk I/O anyway.
    """
    zeros = bytes(_SPARSE_CHUNK)
    with src.open("rb") as fin, dst.open("wb") as fout:
        while chunk := fin.read(_SPARSE_CHUNK):
            if chunk == zeros[: len(chunk)]:
                fout.seek(len(chunk), os.SEEK_CUR)
            else:
                fout.write(chunk)
        # A hole at the end is only a seek until the length is set.
        fout.truncate()
    shutil.copystat(src, dst)


def _move_into_place(built: Path, target: Path) -> None:
    """Rename *built* over *target*; across filesystems, copy it next to
    *target* first, so the last step is still a rename."""
    try:
        os.replace(built, target)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    incoming = target.with_name(f".{target.name}.incoming")
    log.info("GOL: copying %s into place", target.name)
    try:
        _copy_sparse(built, incoming)
        os.replace(incoming, target)
    finally:
        incoming.unlink(missing_ok=True)


def _merge(pbfs: list[Path], output: Path) -> None:
    """``osmium merge | osmium time-filter``, piped so the merged file is
    written once."""
    merge = subprocess.Popen(
        [osmium_binary(), "merge", *map(str, pbfs), "-f", "pbf", "-o", "-"],
        stdout=subprocess.PIPE,
    )
    try:
        subprocess.run(
            [osmium_binary(), "time-filter", "-F", "pbf", "-", "-o", str(output), "--overwrite"],
            stdin=merge.stdout,
            check=True,
        )
    finally:
        merge.stdout.close()
        if merge.wait() != 0:
            raise subprocess.CalledProcessError(merge.returncode, merge.args)


def state_path(target: Path) -> Path:
    return target.with_name(target.name + ".state.txt")


def fingerprint(pbfs: list[Path]) -> list[str]:
    """What identifies the inputs: datasvc replaces a PBF with a rename, so a
    rewritten one has a new mtime."""
    out = []
    for p in pbfs:
        st = p.stat()
        out.append(f"{p.name} {st.st_size} {st.st_mtime_ns}")
    return out


def read_state(target: Path) -> State | None:
    try:
        lines = state_path(target).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    state = State()
    for line in lines:
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key == "timestamp":
            state.timestamp = value
        elif key == "built":
            state.built = value
        elif key == "source":
            state.sources.append(value)
        elif key in ("sequence", "newest"):
            name, _, rest = value.rpartition(" ")
            if not name:
                continue
            if key == "sequence":
                state.sequences[name] = int(rest)
            else:
                state.newest[name] = rest
    return state


def built_from(target: Path) -> list[str] | None:
    state = read_state(target)
    return None if state is None else state.sources


def is_current(pbfs: list[Path], target: Path) -> bool:
    """Whether *target* exists and was built from exactly these PBFs."""
    return target.is_file() and built_from(target) == fingerprint(pbfs)


def _write_state(target: Path, state: State) -> None:
    path = state_path(target)
    incoming = path.with_name(f".{path.name}.incoming")
    incoming.write_text("\n".join(state.lines()) + "\n", encoding="utf-8")
    os.replace(incoming, path)


def newest_object(path: Path) -> str | None:
    """The timestamp of the most recently edited object in *path*, a PBF or
    an ``.osc.gz``.

    A full read, but osmium does it at about a gigabyte a second.
    """
    out = subprocess.run(
        [osmium_binary(), "fileinfo", "-e", "-g", "data.timestamp.last", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    # Objects without a timestamp count as the epoch; osmium reports those
    # as 1970-01-01T00:00:01Z rather than nothing.
    if not out or out.startswith("1970-01-01"):
        return None
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2:
        print("usage: python -m otmlib.gol OUT.gol IN.osm.pbf [IN.osm.pbf ...]", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    build([Path(p) for p in args[1:]], Path(args[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
