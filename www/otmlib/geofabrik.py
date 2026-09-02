"""Geofabrik index lookup, PBF bootstrap and osc.gz replication updates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError

from shapely.geometry import box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from otmlib.proc import check_cancelled, run

GEOFABRIK_BASE_URL = "https://download.geofabrik.de"
INDEX_URL = f"{GEOFABRIK_BASE_URL}/index-v1.json"
CACHE_DIR_NAME = "geofabrik-cache"
INDEX_CACHE_TTL_SEC = 24 * 3600
DOWNLOAD_RETRIES = 3
USER_AGENT = "OpenTopoMap-garmin-server/1.0"
# If bbox fits inside a region smaller than this (deg²), use it directly.
MAX_DIRECT_CONTAINING_AREA = 500.0

_index_cache: dict[str, tuple[float, list["Region"]]] = {}
LogFn = Callable[[str], None]


def mirror_base_url(mirror_dir: Path) -> str:
    """``file://`` root for a local mirror laid out with Geofabrik's own paths.

    Pass the result as ``base_url`` to :func:`load_regions`/:func:`region_by_id`
    to read the index and every extract from *mirror_dir* instead of
    download.geofabrik.de — *mirror_dir* must hold the exact same relative
    paths the site serves (``index-v1.json``, ``russia/…-latest.osm.pbf``,
    ``russia/…-updates/state.txt``, etc).
    """
    return mirror_dir.resolve().as_uri()


def is_not_found_error(exc: BaseException) -> bool:
    """True when *exc* means "this resource doesn't exist", for http(s):// or file:// alike."""
    if isinstance(exc, HTTPError):
        return exc.code == 404
    if isinstance(exc, URLError):
        return isinstance(exc.reason, FileNotFoundError)
    return False


def _rebase_url(url: str, base_url: str) -> str:
    """Swap Geofabrik's own host for *base_url*, keeping the rest of the path."""
    if base_url == GEOFABRIK_BASE_URL or not url.startswith(GEOFABRIK_BASE_URL):
        return url
    return base_url + url[len(GEOFABRIK_BASE_URL) :]


@dataclass(frozen=True)
class Region:
    region_id: str
    name: str
    parent: str | None
    pbf_url: str
    updates_url: str
    geometry: BaseGeometry


def _fetch_index_json(base_url: str) -> dict:
    url = f"{base_url}/index-v1.json"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def _updates_url_from_pbf(pbf_url: str) -> str:
    if pbf_url.endswith("-latest.osm.pbf"):
        return pbf_url[: -len("-latest.osm.pbf")] + "-updates"
    return pbf_url.rsplit("/", 1)[0] + "-updates"


def _parse_regions(data: dict, base_url: str) -> list[Region]:
    regions: list[Region] = []
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        urls = props.get("urls") or {}
        pbf = urls.get("pbf")
        geom = feature.get("geometry")
        if not pbf or not geom:
            continue
        updates = (urls.get("updates") or "").rstrip("/") or _updates_url_from_pbf(pbf)
        regions.append(
            Region(
                region_id=props.get("id", ""),
                name=props.get("name", props.get("id", "")),
                parent=props.get("parent"),
                pbf_url=_rebase_url(pbf, base_url),
                updates_url=_rebase_url(updates, base_url),
                geometry=shape(geom),
            )
        )
    return regions


def _index_cache_filename(base_url: str) -> str:
    if base_url == GEOFABRIK_BASE_URL:
        return "index-v1.json"
    digest = hashlib.sha1(base_url.encode()).hexdigest()[:12]
    return f"index-v1.{digest}.json"


def load_regions(*, cache_dir: Path | None = None, base_url: str = GEOFABRIK_BASE_URL) -> list[Region]:
    """Geofabrik's region index, from *base_url* (download.geofabrik.de by default).

    Pass ``base_url=mirror_base_url(local_dir)`` to read the index and every
    extract from a local mirror laid out with the same paths instead.
    """
    now = time.time()
    cached = _index_cache.get(base_url)
    if cached and now - cached[0] < INDEX_CACHE_TTL_SEC:
        return cached[1]

    cache_file = (cache_dir or Path(".")) / _index_cache_filename(base_url)
    if cache_file.is_file() and now - cache_file.stat().st_mtime < INDEX_CACHE_TTL_SEC:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    else:
        data = _fetch_index_json(base_url)
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(data), encoding="utf-8")

    regions = _parse_regions(data, base_url)
    _index_cache[base_url] = (now, regions)
    return regions


def _ancestor_ids(region_id: str, by_id: dict[str, Region]) -> set[str]:
    chain: set[str] = set()
    current: str | None = region_id
    while current:
        chain.add(current)
        parent = by_id[current].parent if current in by_id else None
        current = parent
    return chain


def find_leaf_regions(
    west: float,
    south: float,
    east: float,
    north: float,
    *,
    cache_dir: Path | None = None,
    base_url: str = GEOFABRIK_BASE_URL,
) -> list[Region]:
    """The smallest set of Geofabrik extracts that covers *bbox*.

    Used by garminsvc, whose bboxes are arbitrary and worldwide — unlike
    tilesvc, which works from an explicit region list in config.yaml.
    """
    if east < west:
        west, east = east, west
    if north < south:
        south, north = north, south

    bbox = box(west, south, east, north)
    regions = load_regions(cache_dir=cache_dir, base_url=base_url)
    by_id = {r.region_id: r for r in regions}
    intersecting = [r for r in regions if r.geometry.intersects(bbox)]
    if not intersecting:
        raise RuntimeError("No Geofabrik region covers the requested bbox")

    # One extract that already contains the bbox beats stitching several, but
    # only while it stays small: "europe" contains most European bboxes too.
    containing = [r for r in intersecting if r.geometry.contains(bbox)]
    if containing:
        best = min(containing, key=lambda r: r.geometry.area)
        if best.geometry.area <= MAX_DIRECT_CONTAINING_AREA:
            return [best]

    pruned: list[Region] = []
    for region in intersecting:
        dominated = False
        for other in intersecting:
            if other.region_id == region.region_id:
                continue
            if region.geometry.contains(other.geometry) and other.geometry.area < region.geometry.area:
                dominated = True
                break
        if dominated:
            continue
        pruned.append(region)

    leaves: list[Region] = []
    for region in pruned:
        is_ancestor = False
        for other in pruned:
            if other.region_id == region.region_id:
                continue
            if region.region_id in _ancestor_ids(other.region_id, by_id):
                is_ancestor = True
                break
        if not is_ancestor:
            leaves.append(region)

    selected = _cover_bbox(bbox, leaves)
    if selected:
        return selected
    if containing:
        return [min(containing, key=lambda r: r.geometry.area)]
    raise RuntimeError("No Geofabrik leaf region covers the requested bbox")


def _cover_bbox(bbox: BaseGeometry, candidates: list[Region]) -> list[Region]:
    """Smallest-first cover so a huge sibling (e.g. Europe) is not pulled in."""
    cover: BaseGeometry | None = None
    selected: list[Region] = []
    seen: set[str] = set()
    for region in sorted(candidates, key=lambda r: r.geometry.area):
        if region.region_id in seen:
            continue
        seen.add(region.region_id)
        piece = region.geometry.intersection(bbox)
        if piece.is_empty:
            continue
        if cover is not None and cover.contains(bbox):
            break
        if cover is not None and cover.covers(piece):
            continue
        selected.append(region)
        cover = piece if cover is None else unary_union([cover, piece])
        if cover.contains(bbox):
            break
    if cover is None or not selected:
        return []
    leftover = bbox.difference(cover).area
    if leftover > bbox.area * 1e-6:
        return []
    return selected


def _fetch_text(url: str, *, timeout: int = 120) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _expected_md5(pbf_url: str) -> str | None:
    try:
        line = _fetch_text(f"{pbf_url}.md5", timeout=30).strip().splitlines()[0]
    except Exception:  # noqa: BLE001
        return None
    match = re.match(r"^[0-9a-fA-F]{32}\b", line)
    return match.group(0).lower() if match else None


def _file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sidecar_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".md5")


def _write_sidecar(path: Path, md5: str) -> None:
    _sidecar_path(path).write_text(f"{md5}  {path.name}\n", encoding="utf-8")


def _require_osmium() -> str:
    path = shutil.which("osmium")
    if not path:
        raise RuntimeError("osmium tool not found; install osmium-tool (brew install osmium-tool)")
    return path


def _seq_relpath(seq: int) -> str:
    """Geofabrik stores sequence 4893 as 000/004/893.osc.gz."""
    padded = f"{seq:09d}"
    return f"{padded[0:3]}/{padded[3:6]}/{padded[6:9]}"


def _osc_url(updates_url: str, seq: int) -> str:
    return f"{updates_url.rstrip('/')}/{_seq_relpath(seq)}.osc.gz"


def _state_url(updates_url: str, seq: int | None = None) -> str:
    base = updates_url.rstrip("/")
    if seq is None:
        return f"{base}/state.txt"
    return f"{base}/{_seq_relpath(seq)}.state.txt"


def _parse_state(text: str) -> tuple[int, str | None]:
    seq: int | None = None
    timestamp: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("sequenceNumber="):
            seq = int(line.split("=", 1)[1].strip())
        elif line.startswith("timestamp="):
            timestamp = line.split("=", 1)[1].strip().replace("\\:", ":")
    if seq is None:
        raise RuntimeError("Geofabrik state.txt has no sequenceNumber")
    return seq, timestamp


def _fetch_state(updates_url: str, seq: int | None = None) -> tuple[int, str | None]:
    return _parse_state(_fetch_text(_state_url(updates_url, seq), timeout=30))


def _parse_geofabrik_ts(value: str) -> datetime:
    cleaned = value.strip().replace("\\:", ":")
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_geofabrik_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def updates_dir(cache_dir: Path, region: Region) -> Path:
    """Where a region's downloaded ``.osc.gz`` diffs live inside the cache."""
    return cache_dir / Path(region.updates_url.rstrip("/")).name


def _osc_local_path(updates_dir: Path, seq: int) -> Path:
    return updates_dir / f"{seq:09d}.osc.gz"


def retain_last_osc(updates_dir: Path, keep_seq: int) -> Path:
    """Drop every downloaded diff but *keep_seq* (kept as a debugging trail)."""
    keep = _osc_local_path(updates_dir, keep_seq)
    if updates_dir.is_dir():
        for path in updates_dir.glob("*.osc.gz"):
            if path.resolve() != keep.resolve():
                path.unlink(missing_ok=True)
    return keep


def _state_sidecar_path(pbf_path: Path) -> Path:
    return Path(str(pbf_path) + ".state.txt")


def _write_pbf_date(pbf_path: Path, seq: int, timestamp: datetime) -> None:
    ts = _format_geofabrik_ts(timestamp).replace(":", "\\:")
    _state_sidecar_path(pbf_path).write_text(
        f"sequenceNumber={seq}\n"
        f"timestamp={ts}\n",
        encoding="utf-8",
    )
    epoch = timestamp.timestamp()
    os.utime(pbf_path, (epoch, epoch))


def _pbf_header_get(pbf_path: Path, key: str) -> str | None:
    osmium = _require_osmium()
    result = subprocess.run(
        [osmium, "fileinfo", "-g", key, str(pbf_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _pbf_header_sequence(pbf_path: Path) -> int | None:
    value = _pbf_header_get(pbf_path, "header.option.osmosis_replication_sequence_number")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _pbf_header_timestamp(pbf_path: Path) -> datetime | None:
    for key in (
        "header.option.osmosis_replication_timestamp",
        "header.option.timestamp",
    ):
        value = _pbf_header_get(pbf_path, key)
        if not value:
            continue
        try:
            return _parse_geofabrik_ts(value)
        except ValueError:
            continue
    return None


def _read_pbf_date(pbf_path: Path) -> tuple[int | None, datetime] | None:
    sidecar = _state_sidecar_path(pbf_path)
    if sidecar.is_file():
        try:
            seq, ts = _parse_state(sidecar.read_text(encoding="utf-8"))
        except (OSError, RuntimeError, ValueError):
            seq, ts = None, None
        if ts:
            return seq, _parse_geofabrik_ts(ts)

    header_ts = _pbf_header_timestamp(pbf_path)
    if header_ts is not None:
        return _pbf_header_sequence(pbf_path), header_ts

    if pbf_path.is_file():
        mtime = datetime.fromtimestamp(pbf_path.stat().st_mtime, tz=timezone.utc)
        return _pbf_header_sequence(pbf_path), mtime
    return None


def _download_file(url: str, dest: Path, log: LogFn | None = None, *, timeout: int = 600) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_exc: Exception | None = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        tmp = dest.with_name(dest.name + ".part")
        if tmp.exists():
            tmp.unlink()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp, tmp.open("wb") as out:
                while True:
                    check_cancelled()
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            if tmp.stat().st_size == 0:
                raise RuntimeError(f"Downloaded empty file: {url}")
            tmp.replace(dest)
            return
        except URLError as exc:
            # HTTPError is a URLError subclass, so this also covers the http(s):// 404 case.
            if tmp.exists():
                tmp.unlink()
            if is_not_found_error(exc):
                raise
            last_exc = exc
            if log:
                log(f"Download attempt {attempt}/{DOWNLOAD_RETRIES} failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if tmp.exists():
                tmp.unlink()
            if log:
                log(f"Download attempt {attempt}/{DOWNLOAD_RETRIES} failed: {exc}")
    raise RuntimeError(f"Failed to download {url}") from last_exc


def _download_full_pbf(url: str, dest: Path, log: LogFn | None = None) -> None:
    if dest.is_file():
        dest.unlink()
    if log:
        log(f"Downloading {url}")
    tmp = dest.with_name(dest.name + ".part")
    _download_file(url, tmp, log)
    expected = _expected_md5(url)
    if expected and _file_md5(tmp) != expected:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded file failed MD5 check: {dest.name}")
    tmp.replace(dest)
    if expected:
        _write_sidecar(dest, expected)
    if log:
        log(f"Cached {dest.name} ({dest.stat().st_size // (1024 * 1024)} MB)")


def _download_osc(
    updates_url: str,
    seq: int,
    updates_dir: Path,
    log: LogFn | None = None,
) -> Path:
    dest = _osc_local_path(updates_dir, seq)
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    url = _osc_url(updates_url, seq)
    if log:
        log(f"Downloading {url}")
    _download_file(url, dest, log, timeout=300)
    return dest


def _latest_new_path(pbf_path: Path) -> Path:
    name = pbf_path.name
    if name.endswith(".osm.pbf"):
        return pbf_path.with_name(name[: -len(".osm.pbf")] + ".new.osm.pbf")
    return pbf_path.with_name(name + ".new.osm.pbf")


def _replace_latest_pbf(new_pbf: Path, latest_pbf: Path) -> None:
    if not new_pbf.is_file() or new_pbf.stat().st_size == 0:
        raise RuntimeError(f"osmium apply-changes produced no output: {new_pbf}")
    os.replace(new_pbf, latest_pbf)
    # The md5 sidecar described the downloaded file, not this rewritten one.
    _sidecar_path(latest_pbf).unlink(missing_ok=True)


def apply_osc_files(pbf_path: Path, osc_files: list[Path]) -> None:
    """Apply *osc_files* onto *pbf_path* in place, via ``osmium apply-changes``.

    osmium cannot write into its own input, so the result goes to a sibling
    ``*.new.osm.pbf`` that replaces the original only once it is complete — a
    crash mid-apply leaves the previous PBF intact and the tracked sequence
    unchanged, so the next run simply retries the same range.
    """
    osmium = _require_osmium()
    new_pbf = _latest_new_path(pbf_path)
    new_pbf.unlink(missing_ok=True)
    try:
        run(
            [
                osmium,
                "apply-changes",
                "-f",
                "pbf",
                "-o",
                str(new_pbf),
                "-O",
                str(pbf_path),
                *[str(p) for p in osc_files],
            ],
        )
        _replace_latest_pbf(new_pbf, pbf_path)
    except Exception:
        new_pbf.unlink(missing_ok=True)
        raise


def extract_bbox(
    source_pbfs: list[Path],
    west: float,
    south: float,
    east: float,
    north: float,
    output_pbf: Path,
) -> Path:
    """Merge (if needed) and cut *bbox* out of Geofabrik PBF files."""
    osmium = _require_osmium()
    output_pbf.parent.mkdir(parents=True, exist_ok=True)
    bbox_arg = f"{west},{south},{east},{north}"

    if len(source_pbfs) == 1:
        merged = source_pbfs[0]
    else:
        merged = output_pbf.parent / "_merged.osm.pbf"
        run([osmium, "merge", "-o", str(merged), "--overwrite", *[str(p) for p in source_pbfs]])

    run(
        [
            osmium,
            "extract",
            # 'smart' keeps multipolygon relations complete across the bbox edge;
            # without it glaciers/forests cut by the border cannot be assembled
            "-s",
            "smart",
            "-b",
            bbox_arg,
            "-o",
            str(output_pbf),
            "--overwrite",
            str(merged),
        ],
    )
    return output_pbf


def merge_pbfs(source_pbfs: list[Path], output_pbf: Path) -> Path:
    """``osmium merge`` several extracts into one tilemaker input.

    A single source is returned as-is: merging one file would only cost a full
    rewrite of a multi-GB extract for no change.
    """
    if not source_pbfs:
        raise ValueError("merge_pbfs: no source PBFs")
    if len(source_pbfs) == 1:
        return source_pbfs[0]
    osmium = _require_osmium()
    output_pbf.parent.mkdir(parents=True, exist_ok=True)
    run([osmium, "merge", "-o", str(output_pbf), "--overwrite", *[str(p) for p in source_pbfs]])
    return output_pbf


def _record_extract_date(
    pbf_path: Path,
    updates_url: str,
    log: LogFn | None = None,
) -> tuple[int | None, datetime | None]:
    seq = _pbf_header_sequence(pbf_path)
    timestamp = _pbf_header_timestamp(pbf_path)
    if seq is None or timestamp is None:
        try:
            latest_seq, latest_ts = _fetch_state(updates_url)
        except Exception:  # noqa: BLE001
            latest_seq, latest_ts = None, None
        if seq is None:
            seq = latest_seq
        if timestamp is None and latest_ts:
            timestamp = _parse_geofabrik_ts(latest_ts)
    if seq is not None and timestamp is not None:
        _write_pbf_date(pbf_path, seq, timestamp)
        if log:
            log(f"{pbf_path.name} date: {_format_geofabrik_ts(timestamp)} (seq {seq})")
    return seq, timestamp


def _region_aliases(region: Region) -> set[str]:
    """Ids a config file may use for *region*.

    Geofabrik's index id is the last path segment (``northwestern-fed-district``).
    The download URL is ``…/russia/northwestern-fed-district-latest.osm.pbf``, and
    that path form is what config.yaml and the docs use.
    """
    aliases = {region.region_id}
    if region.parent:
        aliases.add(f"{region.parent}/{region.region_id}")
    name = Path(region.pbf_url).name
    suffix = "-latest.osm.pbf"
    if name.endswith(suffix):
        stem = name[: -len(suffix)]
        aliases.add(stem)
        parent_dir = Path(region.pbf_url).parent.name
        if parent_dir:
            aliases.add(f"{parent_dir}/{stem}")
    return aliases


def region_by_id(
    region_id: str, *, cache_dir: Path | None = None, base_url: str = GEOFABRIK_BASE_URL
) -> Region:
    """Look up a configured extract, e.g. ``russia/northwestern-fed-district``."""
    wanted = region_id.strip().strip("/")
    for region in load_regions(cache_dir=cache_dir, base_url=base_url):
        if wanted in _region_aliases(region):
            return region
    raise KeyError(f"Unknown Geofabrik region id: {region_id!r}")


def download_full_pbf(region: Region, dest_dir: Path, log: LogFn | None = None) -> Path:
    """A region's full extract, downloaded once into *dest_dir* and cached there.

    This only bootstraps the file. A cache hit is returned as-is and is never
    re-downloaded to pick up a newer version: the cached PBF is brought current
    in place instead, by :func:`apply_osc_files` from the sequence tracked in
    Postgres (see ``otmlib.regionsync``). That file is the tile pipeline's
    input, so keeping it current is the point rather than a side effect.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(region.pbf_url).name
    if not dest.is_file() or dest.stat().st_size == 0:
        _download_full_pbf(region.pbf_url, dest, log)
        _record_extract_date(dest, region.updates_url, log)
    elif _read_pbf_date(dest) is None:
        _record_extract_date(dest, region.updates_url, log)
    return dest


def pbf_sequence(pbf_path: Path) -> int | None:
    """The replication sequence a cached extract is already at, if it says.

    Read from the sidecar Geofabrik's ``state.txt`` was written to, or failing
    that from the PBF's own ``osmosis_replication_sequence_number`` header.
    """
    dated = _read_pbf_date(pbf_path)
    if dated and dated[0] is not None:
        return int(dated[0])
    return None


def refetch_full_pbf(region: Region, dest_dir: Path, log: LogFn | None = None) -> Path:
    """Throw the cached extract away and download it again from scratch.

    The way back when the diff stream cannot be followed: the next diff has
    been rotated out of Geofabrik's window, or so many have piled up that
    applying them one at a time costs more than one fresh download.
    """
    pbf = dest_dir / Path(region.pbf_url).name
    pbf.unlink(missing_ok=True)
    _state_sidecar_path(pbf).unlink(missing_ok=True)
    _sidecar_path(pbf).unlink(missing_ok=True)
    updates = updates_dir(dest_dir, region)
    if updates.is_dir():
        for leftover in updates.glob("*.osc.gz"):
            leftover.unlink(missing_ok=True)
    return download_full_pbf(region, dest_dir, log)


def fetch_latest_sequence(updates_url: str) -> tuple[int, datetime | None]:
    """The most recent sequence/timestamp Geofabrik has published for a region."""
    seq, ts_raw = _fetch_state(updates_url)
    return seq, _parse_geofabrik_ts(ts_raw) if ts_raw else None


def download_osc_range(
    updates_url: str,
    start_seq: int,
    end_seq: int,
    dest_dir: Path,
    log: LogFn | None = None,
) -> list[Path]:
    """Download osc.gz diffs *start_seq*..*end_seq* (inclusive) into *dest_dir*."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    return [_download_osc(updates_url, seq, dest_dir, log) for seq in range(start_seq, end_seq + 1)]
