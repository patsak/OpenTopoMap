"""The terrain class raster: OSM land cover polygons burnt onto the grid."""

from __future__ import annotations

import logging

import numpy as np
import shapely
from osgeo import gdal, ogr
from shapely.geometry.base import BaseGeometry

from routing import model
from routing.grid import Grid, Projector

gdal.UseExceptions()
ogr.UseExceptions()
log = logging.getLogger(__name__)


def memory_layer_source():
    """An in-memory OGR data source: the MEM driver since GDAL 3.11, Memory before."""
    driver = ogr.GetDriverByName("MEM")
    if driver is None or driver.GetMetadataItem("DCAP_VECTOR") != "YES":
        driver = ogr.GetDriverByName("Memory")
    return driver.CreateDataSource("")


def project(geoms, projector: Projector):
    """lon/lat shapely geometries -> the grid's CRS (array in, array out)."""
    def fn(coords: np.ndarray) -> np.ndarray:
        x, y = projector.to_xy(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    return shapely.transform(np.asarray(geoms, dtype=object), fn)


def rasterize(grid: Grid, layers: list[tuple[int, list[BaseGeometry]]], *,
              all_touched: bool = False, dtype=np.uint8, init: int = 0) -> np.ndarray:
    """Burn each (value, polygons) in order, later values over earlier ones."""
    ds = gdal.GetDriverByName("MEM").Create("", grid.width, grid.height, 1,
                                            gdal.GDT_Byte if dtype == np.uint8 else gdal.GDT_Int32)
    ds.SetGeoTransform(grid.geotransform)
    ds.SetProjection(grid.srs().ExportToWkt())
    ds.GetRasterBand(1).Fill(init)
    options = ["ALL_TOUCHED=TRUE"] if all_touched else []
    for value, geoms in layers:
        if not geoms:
            continue
        source = memory_layer_source()
        layer = source.CreateLayer("burn", grid.srs(), ogr.wkbUnknown)
        defn = layer.GetLayerDefn()
        for geom in geoms:
            if geom is None or geom.is_empty:
                continue
            feature = ogr.Feature(defn)
            feature.SetGeometry(ogr.CreateGeometryFromWkb(shapely.to_wkb(geom)))
            layer.CreateFeature(feature)
        gdal.RasterizeLayer(ds, [1], layer, burn_values=[value], options=options)
        del source
    return ds.GetRasterBand(1).ReadAsArray().astype(dtype)


def terrain_raster(grid: Grid, areas_xy: dict[str, list[BaseGeometry]]) -> np.ndarray:
    layers = []
    for code, name in enumerate(model.TERRAINS):
        if name in ("unknown", "path"):
            continue
        layers.append((code, areas_xy.get(name, [])))
    raster = rasterize(grid, layers, init=model.TERRAIN["unknown"])
    counts = np.bincount(raster.ravel(), minlength=len(model.TERRAINS)) / raster.size
    log.info("terrain cover: %s", ", ".join(
        f"{model.TERRAINS[i]} {100 * share:.0f}%" for i, share in enumerate(counts) if share >= 0.005
    ))
    return raster
