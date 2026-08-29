"""Orchestrate a single Garmin map build for a bbox."""

from __future__ import annotations

import logging
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from mapsvc.bbox import BBox, validate_bbox_size
from mapsvc.constants import DEM_CACHE, FAMILY_ID_CONTOURS, FAMILY_ID_MAP, GEOFABRIK_CACHE, ROOT
from mapsvc.dem import prepare_dem_for_bbox
from mapsvc.deps import Deps, require_deps
from mapsvc.geofabrik import download_regions, extract_bbox, find_leaf_regions
from mapsvc.names import garmin_map_names
from mapsvc.pipeline import (
    BuildContext,
    build_contour_pbfs,
    build_contours_map,
    build_crevasse_stripes,
    build_main_map,
    build_ridges,
    compile_typ,
    prepare_pbf_tiles,
    set_contour_area_from_bbox,
    write_bbox_poly,
)
from mapsvc.proc import check_cancelled

log = logging.getLogger(__name__)


@dataclass
class MapPart:
    index: int
    bbox: BBox
    main_img: Path
    contours_img: Path
    main_gmap: Path | None = None
    contours_gmap: Path | None = None


@dataclass
class BuildResult:
    parts: list[MapPart] = field(default_factory=list)
    geofabrik_urls: list[str] = field(default_factory=list)
    zip_path: Path | None = None


class MapBuilder:
    def __init__(self, job_dir: Path, log_fn: Callable[[str], None] | None = None) -> None:
        self.job_dir = job_dir
        self.cache_root = ROOT / "data"
        self.log = log_fn or (lambda msg: log.info("%s", msg))
        self._deps: Deps | None = None
        self._geofabrik_pbfs: list[Path] = []
        self._geofabrik_urls: list[str] = []

    def _ensure_deps(self) -> Deps:
        if self._deps is not None:
            return self._deps
        self.log("Checking dependencies…")
        self._deps = require_deps()
        self.log(f"Sea: {self._deps.sea_dir}")
        self.log(f"Bounds: {self._deps.bounds_dir}")
        return self._deps

    def _prepare_geofabrik(self, bbox: BBox) -> None:
        if self._geofabrik_pbfs:
            return
        regions = find_leaf_regions(
            bbox.west,
            bbox.south,
            bbox.east,
            bbox.north,
            cache_dir=GEOFABRIK_CACHE,
        )
        self._geofabrik_urls = [r.pbf_url for r in regions]
        self.log(f"Geofabrik: {', '.join(r.name for r in regions)}")
        self._geofabrik_pbfs = download_regions(regions, GEOFABRIK_CACHE, self.log)

    def _build_map(
        self,
        bbox: BBox | None,
        *,
        name: str,
        family_id_map: int,
        family_id_contours: int,
        source_pbf: Path | None = None,
    ) -> MapPart:
        check_cancelled()
        deps = self._ensure_deps()
        part_dir = self.job_dir / "build"
        if part_dir.exists():
            shutil.rmtree(part_dir)
        part_dir.mkdir(parents=True)

        map_name, contours_name = garmin_map_names(name)
        self.log(f"Map name: {map_name} (family-id {family_id_map})")
        self.log(f"Contours: {contours_name} (family-id {family_id_contours})")

        ctx = BuildContext(
            work_root=part_dir,
            mkgmap_jar=deps.mkgmap_jar,
            splitter_jar=deps.splitter_jar,
            python=deps.python,
            sea_dir=deps.sea_dir,
            bounds_dir=deps.bounds_dir,
            family_id_map=family_id_map,
            family_id_contours=family_id_contours,
            map_name=map_name,
            contours_name=contours_name,
        )

        pbf_path = part_dir / "data" / "region.osm.pbf"
        pbf_path.parent.mkdir(parents=True, exist_ok=True)

        if source_pbf is not None:
            from mapsvc.osmfile import bbox_from_pbf, to_pbf

            if not source_pbf.is_file():
                raise RuntimeError(f"Uploaded OSM/PBF not found: {source_pbf}")
            self.log(f"Using uploaded file: {source_pbf.name} ({source_pbf.stat().st_size} bytes)")
            to_pbf(source_pbf, pbf_path)
            west, south, east, north = bbox_from_pbf(pbf_path)
            bbox = BBox(west, south, east, north)
            self.log(f"File bbox: {west},{south},{east},{north}")
        else:
            if bbox is None:
                raise RuntimeError("bbox is not set")
            self.log(f"Building: {bbox.west},{bbox.south},{bbox.east},{bbox.north}")
            self._prepare_geofabrik(bbox)
            check_cancelled()
            extract_bbox(
                self._geofabrik_pbfs,
                bbox.west,
                bbox.south,
                bbox.east,
                bbox.north,
                pbf_path,
            )

        ctx.pbf_input = pbf_path
        check_cancelled()

        geotiff, hgt_dir = prepare_dem_for_bbox(
            bbox.west,
            bbox.south,
            bbox.east,
            bbox.north,
            part_dir,
            cache_dir=DEM_CACHE / "hgt",
            log=self.log,
        )
        ctx.hgt_dir = hgt_dir
        ctx.dem_tiff = geotiff
        set_contour_area_from_bbox(ctx, bbox.west, bbox.south, bbox.east, bbox.north)

        poly = write_bbox_poly(
            bbox.west,
            bbox.south,
            bbox.east,
            bbox.north,
            part_dir / "data" / "bbox.poly",
            name="map",
        )
        ctx.polygon_file = poly
        ctx.dem_poly = poly

        artifacts = self.job_dir / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        ctx.output_main_img = artifacts / "otm-hike.img"
        ctx.output_contours_img = artifacts / "otm-hike-contours.img"
        ctx.output_main_gmap = artifacts / "otm-hike.gmap"
        ctx.output_contours_gmap = artifacts / "otm-hike-contours.gmap"

        check_cancelled()
        prepare_pbf_tiles(ctx)
        compile_typ(ctx)
        build_ridges(ctx)
        build_contour_pbfs(ctx)
        build_crevasse_stripes(ctx)
        build_main_map(ctx)
        build_contours_map(ctx)

        assert ctx.output_main_img and ctx.output_contours_img
        return MapPart(
            index=1,
            bbox=bbox,
            main_img=ctx.output_main_img,
            contours_img=ctx.output_contours_img,
            main_gmap=ctx.output_main_gmap,
            contours_gmap=ctx.output_contours_gmap,
        )

    def build(
        self,
        west: float,
        south: float,
        east: float,
        north: float,
        *,
        name: str = "",
        family_id_map: int = FAMILY_ID_MAP,
        family_id_contours: int = FAMILY_ID_CONTOURS,
        source_pbf: Path | None = None,
    ) -> BuildResult:
        bbox: BBox | None = None
        if source_pbf is None:
            if east < west:
                west, east = east, west
            if north < south:
                south, north = north, south
            validate_bbox_size(west, south, east, north)
            bbox = BBox(west, south, east, north)
        part = self._build_map(
            bbox,
            name=name,
            family_id_map=family_id_map,
            family_id_contours=family_id_contours,
            source_pbf=source_pbf,
        )
        zip_path = self._create_zip([part])
        return BuildResult(parts=[part], geofabrik_urls=self._geofabrik_urls, zip_path=zip_path)

    def _create_zip(self, parts: list[MapPart]) -> Path:
        zip_path = self.job_dir / "maps.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for part in parts:
                zf.write(part.main_img, part.main_img.name)
                zf.write(part.contours_img, part.contours_img.name)
                if part.main_gmap and part.main_gmap.is_dir():
                    for file in part.main_gmap.rglob("*"):
                        if file.is_file():
                            zf.write(
                                file,
                                f"{part.main_gmap.name}/{file.relative_to(part.main_gmap)}",
                            )
                if part.contours_gmap and part.contours_gmap.is_dir():
                    for file in part.contours_gmap.rglob("*"):
                        if file.is_file():
                            zf.write(
                                file,
                                f"{part.contours_gmap.name}/{file.relative_to(part.contours_gmap)}",
                            )
        return zip_path
