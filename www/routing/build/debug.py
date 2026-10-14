"""debug.gpkg: the graph as QGIS layers, to see why a route went where it did."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import shapely
from osgeo import ogr, osr

from routing import model

ogr.UseExceptions()


def _wgs84() -> osr.SpatialReference:
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def write(path: Path, arrays: dict, debug: dict, to_lonlat) -> None:
    """*to_lonlat* projects shapely geometries from the grid CRS."""
    source = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    srs = _wgs84()

    layer = source.CreateLayer("edges", srs, ogr.wkbLineString)
    for name, kind in (("kind", ogr.OFTString), ("terrain", ogr.OFTString), ("length", ogr.OFTReal),
                       ("time_fwd", ogr.OFTReal), ("time_bwd", ogr.OFTReal),
                       ("weight_fwd", ogr.OFTReal), ("weight_bwd", ogr.OFTReal),
                       ("kmh_fwd", ogr.OFTReal), ("osm_way", ogr.OFTInteger64)):
        layer.CreateField(ogr.FieldDefn(name, kind))
    defn = layer.GetLayerDefn()
    off = arrays["geom_off"]
    lon, lat = arrays["geom_lon"], arrays["geom_lat"]
    layer.StartTransaction()
    for i in range(len(arrays["edge_u"])):
        f = ogr.Feature(defn)
        f["kind"] = model.KINDS[arrays["edge_kind"][i]]
        f["terrain"] = model.TERRAINS[arrays["edge_terrain"][i]]
        f["length"] = float(arrays["edge_len"][i])
        for name in ("time_fwd", "time_bwd", "weight_fwd", "weight_bwd"):
            value = float(arrays[name][i])
            f[name] = value if np.isfinite(value) else -1.0
        t = float(arrays["time_fwd"][i])
        f["kmh_fwd"] = 3.6 * float(arrays["edge_len"][i]) / t if np.isfinite(t) and t > 0 else 0.0
        f["osm_way"] = int(arrays["edge_osm_way"][i])
        line = ogr.Geometry(ogr.wkbLineString)
        for k in range(off[i], off[i + 1]):
            line.AddPoint_2D(float(lon[k]), float(lat[k]))
        f.SetGeometry(line)
        layer.CreateFeature(f)
    layer.CommitTransaction()

    def polygons(source, name: str, items: list[tuple[object, str]]) -> None:
        layer = source.CreateLayer(name, srs, ogr.wkbUnknown)
        layer.CreateField(ogr.FieldDefn("label", ogr.OFTString))
        defn = layer.GetLayerDefn()
        geoms = to_lonlat([g for g, _ in items]) if items else []
        layer.StartTransaction()
        for geom, (_, label) in zip(geoms, items):
            f = ogr.Feature(defn)
            f["label"] = label
            f.SetGeometry(ogr.CreateGeometryFromWkb(shapely.to_wkb(geom)))
            layer.CreateFeature(f)
        layer.CommitTransaction()

    polygons(source, "barriers", debug.get("barriers", []))
    polygons(source, "canyons", [(g, "canyon floor") for g in debug.get("canyons", [])])
    polygons(source, "pass_zones", [(g, "pass zone") for g in debug.get("pass_zones", [])])
    polygons(source, "crevasses", [(g, "crevasse field") for g in debug.get("crevasses", [])])

    layer = source.CreateLayer("passes", srs, ogr.wkbPoint)
    for name, kind in (("osm_id", ogr.OFTInteger64), ("name", ogr.OFTString), ("descents", ogr.OFTInteger)):
        layer.CreateField(ogr.FieldDefn(name, kind))
    defn = layer.GetLayerDefn()
    passes = debug.get("passes", [])
    if passes:
        pts = to_lonlat([shapely.Point(p["x"], p["y"]) for p in passes])
        for p, pt in zip(passes, pts):
            f = ogr.Feature(defn)
            f["osm_id"] = int(p["id"])
            f["name"] = p["name"]
            f["descents"] = int(p["descents"])
            f.SetGeometry(ogr.CreateGeometryFromWkb(shapely.to_wkb(pt)))
            layer.CreateFeature(f)
    del source
