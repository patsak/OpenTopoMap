"""The GeoDesk GOL www/overpass answers from, built out of the region PBFs.

datasvc builds it as the last step of its nightly pass, right after the sync
(datasvc.job.build_gol), so it follows the extracts without anything watching
them. By hand, from www/:

    python -m otmlib.gol mapsvc/data/gol/armenia.gol \\
        mapsvc/data/geofabrik-cache/armenia-latest.osm.pbf

Several regions become one GOL, not one per region. A query then sees one
dataset - a relation or a way crossing a district border is one element, as
in Overpass, instead of two clipped halves in two files. They go through
``osmium merge`` and then ``osmium time-filter``: merge drops an object
repeated across extracts only when it is the same version in each, so a
region that missed a night's diffs would otherwise put two versions of
everything along its border into the GOL. time-filter keeps the newest.

The GOL is renamed over the previous one only once it is complete, so the
service never opens a half-written file - it sees the new one on its next
query (overpass.library). It is built in ``OTM_GOL_WORK_DIR`` when that is set,
else next to its final place. The work dir exists for data dirs gol cannot
build on: Colima on macOS mounts the host's directories into its VM over
sshfs, which refuses an operation gol needs ("Operation not supported"), so
there the GOL is built on the container's own disk and copied over.

Next to the GOL goes ``<name>.gol.state.txt``:

* ``timestamp=`` - Overpass's ``timestamp_osm_base``, the newest edit in each
  PBF and the oldest of those across them, since the whole is only as fresh
  as its stalest part. Read out of the objects by osmium: datasvc applies the
  diffs in place and keeps the sequence in Postgres, so the PBF header and
  Geofabrik's state.txt beside it still describe the first download.
* ``source=`` - name, size and mtime of each PBF, which is how
  ``is_current`` knows a GOL was built from exactly these files.

``-w`` (waynode IDs) is not optional: without it every untagged way node has
id 0, and the service refuses the GOL.
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
from pathlib import Path

log = logging.getLogger(__name__)

# Free space a build needs in its work dir, as a multiple of the input PBFs'
# total size: the merged PBF, gol's working files and the GOL it writes, at
# their peak. Measured on the nine Russian federal districts: 4.2 GB of PBF,
# 27.7 GB at the peak (6.6x) - gol's sort files dwarf the 6.6 GB GOL it ends
# with. 8x leaves a margin.
WORK_SPACE_FACTOR = 8.0


def gol_binary() -> str:
    return os.environ.get("OTM_GOL_BIN", "gol")


def osmium_binary() -> str:
    return os.environ.get("OTM_OSMIUM_BIN", "osmium")


def build(pbfs: list[Path], target: Path) -> None:
    missing = [p for p in pbfs if not p.is_file()]
    if missing:
        raise FileNotFoundError("missing: " + ", ".join(str(p) for p in missing))
    # Taken before reading, so a PBF rewritten meanwhile reads as changed.
    sources = fingerprint(pbfs)
    target.parent.mkdir(parents=True, exist_ok=True)
    work_root = work_dir() or target.parent
    work_root.mkdir(parents=True, exist_ok=True)
    _require_space(pbfs, work_root)
    with tempfile.TemporaryDirectory(dir=work_root, prefix=".building-") as work:
        source = pbfs[0]
        if len(pbfs) > 1:
            source = Path(work) / "merged.osm.pbf"
            log.info("GOL: merging %d extracts", len(pbfs))
            _merge(pbfs, source)
        # gol appends ".gol" to the name it is given.
        scratch = Path(work) / "library"
        log.info("GOL: building %s", target)
        _run_gol_build(scratch, source, work_root)
        _move_into_place(scratch.with_suffix(".gol"), target)
    _write_state(pbfs, target, sources)
    log.info("GOL: %s is ready", target)


class GolBuildError(RuntimeError):
    pass


def _require_space(pbfs: list[Path], work_root: Path) -> None:
    """Refuse up front rather than let gol run out of disk half-way.

    gol maps its working files into memory, so a full disk does not reach it
    as ENOSPC: the kernel kills it with SIGBUS, hours into a build on a slow
    machine, with nothing to say why.
    """
    need = int(sum(p.stat().st_size for p in pbfs) * WORK_SPACE_FACTOR)
    free = shutil.disk_usage(work_root).free
    if free < need:
        raise GolBuildError(
            f"building a GOL from {_gb(sum(p.stat().st_size for p in pbfs))} of PBF needs about "
            f"{_gb(need)} free in {work_root}; there is {_gb(free)}"
        )


def _run_gol_build(scratch: Path, source: Path, work_root: Path) -> None:
    try:
        subprocess.run([gol_binary(), "build", str(scratch), str(source), "-w"], check=True)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == -signal.SIGBUS:
            raise GolBuildError(
                f"gol died with SIGBUS: {work_root} most likely ran out of space "
                f"(there is {_gb(shutil.disk_usage(work_root).free)} free now that its files are gone)"
            ) from exc
        raise


def _gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"


def work_dir() -> Path | None:
    configured = os.environ.get("OTM_GOL_WORK_DIR", "").strip()
    return Path(configured) if configured else None


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
        shutil.copyfile(built, incoming)
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


def built_from(target: Path) -> list[str] | None:
    try:
        lines = state_path(target).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    return [line.split("=", 1)[1] for line in lines if line.startswith("source=")]


def is_current(pbfs: list[Path], target: Path) -> bool:
    """Whether *target* exists and was built from exactly these PBFs."""
    return target.is_file() and built_from(target) == fingerprint(pbfs)


def _write_state(pbfs: list[Path], target: Path, sources: list[str]) -> None:
    lines = []
    stamps = [newest_object(p) for p in pbfs]
    # Unknown for one part means unknown for the whole: overpass.library then
    # falls back to the GOL's mtime.
    if all(s is not None for s in stamps):
        # ISO-8601 in UTC with a Z compares correctly as text.
        lines.append(f"timestamp={min(stamps)}")
    lines += [f"source={s}" for s in sources]
    state_path(target).write_text("\n".join(lines) + "\n", encoding="utf-8")


def newest_object(pbf: Path) -> str | None:
    """The timestamp of the most recently edited object in *pbf*.

    A full read, but osmium does it at about a gigabyte a second.
    """
    out = subprocess.run(
        [osmium_binary(), "fileinfo", "-e", "-g", "data.timestamp.last", str(pbf)],
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
