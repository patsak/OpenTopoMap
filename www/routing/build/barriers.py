"""What an off-trail edge may not cross, and the streams it fords on the way.

Every barrier is a polygon in the grid's CRS, labelled with what it is:

water    lakes and river areas from OSM, and rivers too big to ford -
         OSM waterways and DEM channels buffered to their width
cliff    natural=cliff from OSM (areas, lines buffered) and patches of the
         DEM steeper than cliff_slope_deg
canyon   gorges from OSM, and DEM valley floors with steep high walls on both
         sides, as a corridor as wide as the walls
waterfall a small disc around waterway=waterfall: the valley floor breaks there
line     the other barrier_lines of the config (fences, railways, motorways)

Only an OSM way or a marked ford crosses them. Large polygons are cut into
tiles before indexing, so testing an edge against a lake shore costs a tile's
worth of vertices, not the whole lake.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import shapely
import shapely.ops
from scipy import ndimage
from shapely import STRtree
from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry

log = logging.getLogger(__name__)

TILE_M = 2000.0
TILE_ABOVE_VERTICES = 256


def river_width_m(catchment_km2) -> np.ndarray:
    """A width for a river nobody measured: hydraulic geometry, w ~ A^0.5,
    about 3 m at 10 km2 and 30 m at 1000 km2."""
    return np.clip(1.0 * np.sqrt(np.asarray(catchment_km2, dtype=np.float64)), 2.0, 200.0)


@dataclass
class Barriers:
    geoms: list[BaseGeometry] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    _tree: STRtree | None = None
    _tile_geoms: np.ndarray | None = None
    _tile_labels: np.ndarray | None = None

    def add(self, geoms, label: str) -> None:
        for geom in geoms:
            if geom is None or geom.is_empty:
                continue
            for part in getattr(geom, "geoms", [geom]):
                if isinstance(part, Polygon) and not part.is_empty:
                    self.geoms.append(part)
                    self.labels.append(label)
        self._tree = None

    def count(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for label in self.labels:
            out[label] = out.get(label, 0) + 1
        return out

    def _index(self) -> None:
        if self._tree is not None:
            return
        tiles: list[BaseGeometry] = []
        labels: list[str] = []
        for geom, label in zip(self.geoms, self.labels):
            if shapely.get_num_coordinates(geom) <= TILE_ABOVE_VERTICES:
                tiles.append(geom)
                labels.append(label)
                continue
            x0, y0, x1, y1 = geom.bounds
            for tx in np.arange(x0, x1, TILE_M):
                for ty in np.arange(y0, y1, TILE_M):
                    piece = shapely.clip_by_rect(geom, tx, ty, tx + TILE_M, ty + TILE_M)
                    if not piece.is_empty:
                        tiles.append(piece)
                        labels.append(label)
        self._tile_geoms = np.asarray(tiles, dtype=object)
        self._tile_labels = np.asarray(labels, dtype=object)
        shapely.prepare(self._tile_geoms)
        self._tree = STRtree(self._tile_geoms)

    def blocked(self, lines, only: tuple[str, ...] = (), ignore: tuple[str, ...] = ()) -> np.ndarray:
        """Which of *lines* (or points) touch a barrier - of the *only* labels,
        if given, and not of the *ignore* ones."""
        lines = np.asarray(lines, dtype=object)
        out = np.zeros(len(lines), dtype=bool)
        if not self.geoms or len(lines) == 0:
            return out
        self._index()
        hit_line, hit_tile = self._tree.query(lines, predicate="intersects")
        if only:
            keep = np.isin(self._tile_labels[hit_tile], only)
            hit_line, hit_tile = hit_line[keep], hit_tile[keep]
        if ignore:
            hit_line = hit_line[~np.isin(self._tile_labels[hit_tile], ignore)]
        out[hit_line] = True
        return out



# ----------------------------------------------------------- DEM obstacles


def dem_cliffs(slope_deg: np.ndarray, grid, threshold_deg: float, min_cells: int) -> list[Polygon]:
    """Patches of ground steeper than *threshold_deg*, as polygons."""
    from osgeo import gdal, ogr

    from routing.build.terrain import memory_layer_source

    mask = np.nan_to_num(slope_deg) > threshold_deg
    labels, count = ndimage.label(mask, structure=np.ones((3, 3)))
    if count == 0:
        return []
    sizes = np.bincount(labels.ravel())
    keep = sizes >= min_cells
    keep[0] = False
    mask = keep[labels]
    if not mask.any():
        return []
    ds = gdal.GetDriverByName("MEM").Create("", grid.width, grid.height, 1, gdal.GDT_Byte)
    ds.SetGeoTransform(grid.geotransform)
    ds.SetProjection(grid.srs().ExportToWkt())
    band = ds.GetRasterBand(1)
    band.WriteArray(mask.astype(np.uint8))
    source = memory_layer_source()
    layer = source.CreateLayer("cliffs", grid.srs(), ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    gdal.Polygonize(band, band, layer, 0)
    out = []
    for feature in layer:
        geom = feature.GetGeometryRef()
        if geom is not None:
            out.append(shapely.from_wkb(bytes(geom.ExportToWkb())))
    return out


def chain_line(cells: np.ndarray, grid) -> LineString:
    rows, cols = np.divmod(cells, grid.width)
    x, y = grid.cell_xy(rows, cols)
    return LineString(np.column_stack([x, y]))


def segment_lines(line: LineString, piece_m: float) -> list[LineString]:
    """*line* cut into consecutive pieces of about *piece_m*."""
    length = line.length
    if length <= piece_m:
        return [line]
    n = int(np.ceil(length / piece_m))
    marks = np.linspace(0.0, length, n + 1)
    return [shapely.ops.substring(line, a, b) for a, b in zip(marks[:-1], marks[1:])]


def bank_lines(water: list[BaseGeometry], offset_m: float, clip: tuple[float, ...]) -> list[LineString]:
    """The walkable near bank of every water barrier: its outline pushed
    *offset_m* out onto dry ground, inside the *clip* box."""
    if not water:
        return []
    union = shapely.union_all(np.asarray(water, dtype=object))
    grown = union.buffer(offset_m, quad_segs=4)
    outline = shapely.clip_by_rect(grown.boundary, *clip).simplify(5.0)
    lines = []
    for part in getattr(outline, "geoms", [outline]):
        if isinstance(part, LineString) and part.length > 0:
            lines.append(part)
    return lines
