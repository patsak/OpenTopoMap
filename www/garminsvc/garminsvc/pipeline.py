"""Garmin map build pipeline."""

from __future__ import annotations

import logging
import os
import shutil
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from garminsvc.constants import (
    DEM_DISTS,
    FAMILY_ID_CONTOURS,
    FAMILY_ID_MAP,
    OPTIONS_CONTOURS,
    OPTIONS_MAIN,
    RIDGES_SCRIPT,
    ROOT,
    STYLE_DIR,
)
from garminsvc.proc import check_cancelled, run, worker_count

log = logging.getLogger(__name__)

# id space reserved per DEM tile for pyhgtmap output (~1.5M nodes per 1°x1° tile)
ID_BASE = 10_000_000
ID_STRIDE = 100_000_000
CONTOUR_JOBS_ENV = "OTM_PYHGTMAP_JOBS"


@dataclass
class BuildContext:
    work_root: Path
    java_mem: str = field(default_factory=lambda: os.environ.get("JAVA_MEM", "4g"))
    mkgmap_jar: Path = field(default_factory=lambda: ROOT / "tools" / "mkgmap.jar")
    splitter_jar: Optional[Path] = None
    python: str = field(default_factory=lambda: __import__("sys").executable)

    pbf_input: Path | None = None
    hgt_dir: Path | None = None
    dem_tiff: Optional[Path] = None
    dem_poly: Optional[Path] = None
    polygon_file: Optional[Path] = None

    family_id_map: int = FAMILY_ID_MAP
    family_id_contours: int = FAMILY_ID_CONTOURS
    map_name: str = "OpenTopoMap Hike"
    contours_name: str = "OpenTopoMap Contours Hike"
    output_main_img: Optional[Path] = None
    output_contours_img: Optional[Path] = None
    output_main_gmap: Optional[Path] = None
    output_contours_gmap: Optional[Path] = None

    sea_dir: Optional[Path] = None
    bounds_dir: Optional[Path] = None
    split_dir: Optional[Path] = None
    source_pbf: Optional[Path] = None
    contour_area: str = ""
    contours_dir: Optional[Path] = None
    ridges_osm: Optional[Path] = None
    crevasse_osm: Optional[Path] = None

    @property
    def data_dir(self) -> Path:
        return self.work_root / "data"

    @property
    def output_root(self) -> Path:
        return self.work_root / "output"


def first_glob(path: Path, pattern: str) -> list[Path]:
    return sorted(path.glob(pattern))


def copy_gmap(src_dir: Path, dest: Path, preferred_names: Iterable[str]) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    for name in preferred_names:
        candidate = src_dir / name
        if candidate.is_dir():
            shutil.copytree(candidate, dest)
            return
    found = list(src_dir.glob("*.gmap"))
    if found:
        shutil.copytree(found[0], dest)


def write_bbox_poly(
    west: float,
    south: float,
    east: float,
    north: float,
    path: Path,
    *,
    name: str = "bbox",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{name}\n"
        f"1\n"
        f"  {west} {south}\n"
        f"  {east} {south}\n"
        f"  {east} {north}\n"
        f"  {west} {north}\n"
        f"  {west} {south}\n"
        f"END\n"
        f"END\n",
        encoding="utf-8",
    )
    return path


def set_contour_area_from_bbox(
    ctx: BuildContext,
    west: float,
    south: float,
    east: float,
    north: float,
) -> None:
    ctx.contour_area = f"{west}:{south}:{east}:{north}"


def prepare_pbf_tiles(ctx: BuildContext) -> None:
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    split = ctx.data_dir / "split"
    tile_glob = f"{ctx.family_id_map}*.osm.pbf"
    pbf = ctx.pbf_input
    if pbf is None:
        raise RuntimeError("pbf_input is not set")
    if not pbf.is_file():
        raise RuntimeError(f"PBF not found: {pbf}")

    if split.exists():
        shutil.rmtree(split)
    split.mkdir(parents=True)
    if ctx.splitter_jar is None or ctx.sea_dir is None:
        raise RuntimeError("splitter_jar / sea_dir required")

    cmd = [
        "java",
        f"-Xmx{ctx.java_mem}",  # heap JVM (JAVA_MEM, по умолчанию 4g)
        "-jar",
        str(ctx.splitter_jar),
        # Готовая береговая линия: корректный разрез тайлов по морю
        f"--precomp-sea={ctx.sea_dir}",
        f"--output-dir={split}",
        f"--mapid={ctx.family_id_map}0001",
    ]
    if ctx.polygon_file and ctx.polygon_file.is_file():
        # Обрезать OSM по полигону bbox, не резать весь extract
        cmd.append(f"--polygon-file={ctx.polygon_file}")
    cmd.append(str(pbf))
    run(cmd)
    ctx.split_dir = split
    ctx.source_pbf = pbf
    if not first_glob(split, tile_glob):
        raise RuntimeError(f"No tiles {tile_glob} after splitter in {split}")


def compile_typ(ctx: BuildContext) -> None:
    typ_dir = ctx.work_root / "typ"
    typ_dir.mkdir(parents=True, exist_ok=True)
    map_src = STYLE_DIR / "typ" / "opentopomap-hike.txt"
    contours_src = STYLE_DIR / "typ" / "contours-hike.txt"
    map_typ = typ_dir / "opentopomap-hike.typ"
    contours_typ = typ_dir / "contours-hike.typ"

    def _compile(src: Path, dest: Path, family_id: int) -> None:
        # TYP: стили отрисовки объектов Garmin; family-id должен совпадать с картой
        run(["java", "-jar", str(ctx.mkgmap_jar), f"--family-id={family_id}", str(src)], cwd=typ_dir)
        candidates = [
            typ_dir / f"{src.stem}.typ",
            ROOT / f"{src.stem}.typ",
            src.with_suffix(".typ"),
        ]
        for candidate in candidates:
            if candidate.is_file():
                if candidate.resolve() != dest.resolve():
                    shutil.move(str(candidate), str(dest))
                break
        if not dest.is_file():
            raise RuntimeError(f"TYP not produced for {src.name}")

    _compile(map_src, map_typ, ctx.family_id_map)
    _compile(contours_src, contours_typ, ctx.family_id_contours)


def build_ridges(ctx: BuildContext) -> None:
    src = ctx.source_pbf or ctx.pbf_input
    if src is None or not src.is_file():
        log.warning("No source PBF for ridges — skipping")
        ctx.ridges_osm = None
        return
    out_dir = ctx.data_dir / "ridges-hike"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "ridges.osm"
    run(
        [
            ctx.python,
            str(RIDGES_SCRIPT),
            "--osm-pbf",
            str(src),
            "--output",
            str(out),
        ]
    )
    ctx.ridges_osm = out


def build_crevasse_stripes(ctx: BuildContext) -> None:
    src = ctx.source_pbf or ctx.pbf_input
    if src is None or not src.is_file():
        log.warning("No source PBF for crevasse stripes — skipping")
        ctx.crevasse_osm = None
        return
    if ctx.contours_dir is None:
        log.warning("No contour dir for crevasse hatch — skipping")
        ctx.crevasse_osm = None
        return
    contour_pbfs = first_glob(ctx.contours_dir, "*.osm.pbf")
    if not contour_pbfs:
        log.warning("No contour PBF for crevasse hatch — skipping")
        ctx.crevasse_osm = None
        return
    from garminsvc.crevasse import build_crevasse_stripes as _build

    log.info("Crevasse hatch along DEM contours: %s", src)
    out = ctx.data_dir / "crevasse-stripes.osm"
    ctx.crevasse_osm = _build(src, contour_pbfs, out)
    if ctx.crevasse_osm:
        log.info("Crevasse stripes: %s", ctx.crevasse_osm)
    else:
        log.info("No crevasse stripes (no contour lines inside crevasse areas)")


def _outside_bbox(pbf: Path, area: str) -> bool:
    from garminsvc.osm_areas import pbf_bbox

    try:
        west, south, east, north = (float(x) for x in area.split(":"))
    except ValueError:
        return False
    box = pbf_bbox(pbf)
    if box is None:
        return False
    p_west, p_south, p_east, p_north = box
    return p_east < west or p_west > east or p_north < south or p_south > north


def _generate_contours(dem: str, out_dir: str, step: int, start_id: int, prefix: str) -> list[str]:
    """One pyhgtmap run; chdir keeps its relative output paths inside *out_dir*."""
    from pyhgtmap.main import main_internal

    prev = os.getcwd()
    os.chdir(out_dir)
    try:
        main_internal(
            [
                f"--step={step}",
                "--line-cat=100,50",
                "--pbf",
                f"--start-node-id={start_id}",
                f"--start-way-id={start_id}",
                f"--output-prefix={prefix}",
                dem,
            ]
        )
    finally:
        os.chdir(prev)
    return [str(p) for p in sorted(Path(out_dir).glob(f"{prefix}*.osm.pbf"))]


def build_contour_pbfs(ctx: BuildContext) -> None:
    from garminsvc.contour_terrain import contour_step

    contours = ctx.data_dir / "contours-hike"
    contours.mkdir(parents=True, exist_ok=True)
    # kept absolute: pyhgtmap runs under chdir(contours), where relative paths would break
    contours = contours.resolve()
    for p in contours.glob("*.osm.pbf"):
        p.unlink()

    if not ctx.contour_area:
        raise RuntimeError("contour_area is not set")

    dem_files: list[Path] = []
    if ctx.dem_tiff and ctx.dem_tiff.is_file():
        dem_files = [ctx.dem_tiff]
    elif ctx.hgt_dir:
        dem_files = sorted(ctx.hgt_dir.glob("*.hgt"))
    if not dem_files:
        raise RuntimeError("No DEM (.tif / .hgt) for contours")
    dem_files = [p.resolve() for p in dem_files]

    check_cancelled()
    # pyhgtmap restarts ids at 10M on every run, so per-tile runs would collide
    # and osmium/mkgmap would silently drop the duplicates
    tasks = [
        (str(dem), str(contours), contour_step(dem), ID_BASE + i * ID_STRIDE, f"contours-{dem.stem}")
        for i, dem in enumerate(dem_files)
    ]
    jobs = worker_count(len(tasks), CONTOUR_JOBS_ENV)
    log.info("pyhgtmap: %s DEM tiles on %s workers", len(tasks), jobs)
    generated: list[Path] = []
    if jobs == 1:
        for task in tasks:
            check_cancelled()
            generated.extend(Path(p) for p in _generate_contours(*task))
    else:
        pool = ProcessPoolExecutor(max_workers=jobs)
        try:
            pending = {pool.submit(_generate_contours, *task): task for task in tasks}
            for future in as_completed(pending):
                generated.extend(Path(p) for p in future.result())
                check_cancelled()
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown()
    generated.sort()

    if not generated:
        raise RuntimeError(f"pyhgtmap produced no contour PBF in {contours}")

    # pyhgtmap ignores --area for explicit DEM files and covers whole tiles,
    # so drop the bands that miss the build bbox entirely
    kept: list[Path] = []
    for pbf in generated:
        if _outside_bbox(pbf, ctx.contour_area):
            pbf.unlink(missing_ok=True)
        else:
            kept.append(pbf)
    if not kept:
        raise RuntimeError(f"All contour PBFs fall outside {ctx.contour_area}")
    log.info("Contour PBFs: %s kept of %s generated", len(kept), len(generated))

    ctx.contours_dir = contours

    from garminsvc.contour_post import postprocess_contour_pbfs

    osm_src = ctx.source_pbf or ctx.pbf_input
    postprocess_contour_pbfs(kept, osm_src, dem_files)


def build_main_map(ctx: BuildContext) -> None:
    out = ctx.output_root / "hike"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    if not (ctx.split_dir and ctx.sea_dir and ctx.bounds_dir and ctx.hgt_dir):
        raise RuntimeError("split_dir / sea_dir / bounds_dir / hgt_dir required")

    tile_glob = f"{ctx.family_id_map}*.osm.pbf"
    tiles = first_glob(ctx.split_dir, tile_glob)
    if not tiles:
        raise RuntimeError(f"No tiles {tile_glob} in {ctx.split_dir}")

    typ_path = ctx.work_root / "typ" / "opentopomap-hike.typ"
    if not typ_path.is_file():
        raise RuntimeError(f"Missing TYP: {typ_path}")

    cmd = [
        "java",
        f"-Xmx{ctx.java_mem}",  # heap JVM
        "-jar",
        str(ctx.mkgmap_jar),
        "-c",  # общие опции mkgmap (gmapsupp, index, routing, …)
        str(OPTIONS_MAIN),
        f"--style-file={STYLE_DIR / 'opentopomap-hike'}",  # правила OSM → Garmin
        f"--precomp-sea={ctx.sea_dir}",  # полигоны моря
        f"--bounds={ctx.bounds_dir}",  # административные границы / адреса
        f"--dem={ctx.hgt_dir}",  # SRTM .hgt для рельефа
        f"--dem-dists={DEM_DISTS}",  # шаг DEM по уровням zoom (метры)
        "--show-profiles=1",  # профили высот на устройстве
        "--gmapi",  # каталог .gmap для BaseCamp
        f"--family-id={ctx.family_id_map}",  # должен совпадать с TYP и splitter mapid
        f"--family-name={ctx.map_name}",
        f"--series-name={ctx.map_name}",
        f"--area-name={ctx.map_name}",
        f"--description={ctx.map_name}",
        f"--output-dir={out}",
    ]
    if ctx.dem_poly and ctx.dem_poly.is_file():
        # Обрезать DEM по тому же полигону, что и OSM
        cmd.append(f"--dem-poly={ctx.dem_poly}")
    cmd.extend(str(t) for t in tiles)
    if ctx.crevasse_osm and ctx.crevasse_osm.is_file():
        cmd.append(str(ctx.crevasse_osm))
    cmd.append(str(typ_path))
    run(cmd)

    ctx.output_root.mkdir(parents=True, exist_ok=True)
    main_img = ctx.output_main_img or (ctx.output_root / "otm-hike.img")
    main_gmap = ctx.output_main_gmap or (ctx.output_root / "otm-hike.gmap")
    gmapsupp = out / "gmapsupp.img"
    if not gmapsupp.is_file():
        raise RuntimeError(f"mkgmap did not produce {gmapsupp}")
    shutil.copy2(gmapsupp, main_img)
    copy_gmap(out, main_gmap, [f"{ctx.map_name}.gmap"])
    ctx.output_main_img = main_img
    ctx.output_main_gmap = main_gmap


def build_contours_map(ctx: BuildContext) -> None:
    out = ctx.output_root / "hike-contours"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    if ctx.contours_dir is None:
        raise RuntimeError("contours_dir is not set")
    contour_files = first_glob(ctx.contours_dir, "*.osm.pbf")
    if not contour_files:
        raise RuntimeError(f"No contour PBF in {ctx.contours_dir}")

    extras: list[str] = []
    if ctx.ridges_osm and ctx.ridges_osm.is_file():
        extras.append(str(ctx.ridges_osm))

    contours_typ = ctx.work_root / "typ" / "contours-hike.typ"
    if not contours_typ.is_file():
        raise RuntimeError(f"Missing TYP: {contours_typ}")

    run(
        [
            "java",
            f"-Xmx{ctx.java_mem}",  # heap JVM
            "-jar",
            str(ctx.mkgmap_jar),
            "-c",  # опции изолиний (без routing/index основной карты)
            str(OPTIONS_CONTOURS),
            f"--style-file={STYLE_DIR / 'contours-hike'}",
            f"--family-id={ctx.family_id_contours}",
            f"--mapname={ctx.family_id_contours}0001",  # ID основного контурного тайла
            f"--overview-mapname={ctx.family_id_contours}0000",  # overview-карта
            f"--description={ctx.contours_name}",
            f"--family-name={ctx.contours_name}",
            f"--series-name={ctx.contours_name}",
            f"--area-name={ctx.contours_name}",
            "--gmapi",  # каталог .gmap для BaseCamp
            f"--output-dir={out}",
            *[str(p) for p in contour_files],
            *extras,  # ridges.osm, если собран
            str(contours_typ),
        ]
    )

    contours_img = ctx.output_contours_img or (ctx.output_root / "otm-hike-contours.img")
    contours_gmap = ctx.output_contours_gmap or (ctx.output_root / "otm-hike-contours.gmap")
    gmapsupp = out / "gmapsupp.img"
    if not gmapsupp.is_file():
        raise RuntimeError(f"mkgmap did not produce {gmapsupp}")
    shutil.copy2(gmapsupp, contours_img)
    copy_gmap(out, contours_gmap, [f"{ctx.contours_name}.gmap"])
    ctx.output_contours_img = contours_img
    ctx.output_contours_gmap = contours_gmap
