"""The graph package: what a build leaves in ``data/routing/<area>/``.

    graph.npz   nodes, edges with both-way times and weights, edge geometry
    meta.json   schema, area, sources, fingerprint, counts
    debug.gpkg  edges, barriers, passes and canyons, for QGIS (optional)

The directory is written next to the live one and swapped in with renames, so
the server, which reloads when graph.npz changes, never sees half a package.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from routing import model

GRAPH = "graph.npz"
META = "meta.json"
DEBUG = "debug.gpkg"

ARRAYS = (
    "node_lon", "node_lat", "node_x", "node_y", "node_ele", "node_kind", "node_osm",
    "edge_u", "edge_v", "edge_kind", "edge_terrain", "edge_len",
    "time_fwd", "time_bwd", "weight_fwd", "weight_bwd", "edge_osm_way", "edge_extra", "edge_pass", "edge_cross",
    "pass_osm", "pass_grade", "pass_lon", "pass_lat",
    "climb_fwd", "climb_bwd", "descent_fwd", "descent_bwd", "rough_fwd", "rough_bwd",
    "steep_fwd", "steep_bwd",
    "geom_off", "geom_lon", "geom_lat", "geom_ele",
)


class PackageError(Exception):
    pass


@dataclass
class Package:
    meta: dict
    node_lon: np.ndarray
    node_lat: np.ndarray
    node_x: np.ndarray
    node_y: np.ndarray
    node_ele: np.ndarray
    node_kind: np.ndarray
    node_osm: np.ndarray
    edge_u: np.ndarray
    edge_v: np.ndarray
    edge_kind: np.ndarray
    edge_terrain: np.ndarray
    edge_len: np.ndarray
    time_fwd: np.ndarray
    time_bwd: np.ndarray
    weight_fwd: np.ndarray
    weight_bwd: np.ndarray
    edge_osm_way: np.ndarray
    edge_extra: np.ndarray  # seconds of weight that are not walking: wading, stepping off a trail
    edge_pass: np.ndarray  # int32 index into pass_*: the pass the edge needs, -1 none
    edge_cross: np.ndarray  # int32: the pass the edge goes over (counted), -1 none
    pass_osm: np.ndarray
    pass_grade: np.ndarray  # model.GRADES index, model.UNRATED
    pass_lon: np.ndarray
    pass_lat: np.ndarray
    climb_fwd: np.ndarray  # see routing.cost.EdgeCosts and routing.pack
    climb_bwd: np.ndarray
    descent_fwd: np.ndarray
    descent_bwd: np.ndarray
    rough_fwd: np.ndarray
    rough_bwd: np.ndarray
    steep_fwd: np.ndarray  # the steep-descent part of weight_*, seconds (routing.pack)
    steep_bwd: np.ndarray
    geom_off: np.ndarray
    geom_lon: np.ndarray
    geom_lat: np.ndarray
    geom_ele: np.ndarray

    @property
    def epsg(self) -> int:
        return int(self.meta["epsg"])

    @property
    def node_count(self) -> int:
        return len(self.node_x)

    @property
    def edge_count(self) -> int:
        return len(self.edge_u)

    def edge_coords(self, i: int, forward: bool = True) -> np.ndarray:
        """(lon, lat, ele) of edge *i*'s polyline, in the direction walked."""
        lo, hi = self.geom_off[i], self.geom_off[i + 1]
        coords = np.column_stack([self.geom_lon[lo:hi], self.geom_lat[lo:hi], self.geom_ele[lo:hi]])
        return coords if forward else coords[::-1]


def write(out_dir: Path, arrays: dict[str, np.ndarray], meta: dict,
          debug_writer=None) -> Path:
    missing = [name for name in ARRAYS if name not in arrays]
    if missing:
        raise ValueError(f"package arrays missing: {', '.join(missing)}")
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = out_dir.with_name(f".{out_dir.name}.new")
    retired = out_dir.with_name(f".{out_dir.name}.old")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    # compressed: half the size, and the load is still a fraction of a second
    np.savez_compressed(staging / GRAPH, **{name: arrays[name] for name in ARRAYS})
    meta = {**meta, "schema": model.SCHEMA}
    (staging / META).write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    if debug_writer is not None:
        debug_writer(staging / DEBUG)
    shutil.rmtree(retired, ignore_errors=True)
    if out_dir.exists():
        out_dir.rename(retired)
    staging.rename(out_dir)
    shutil.rmtree(retired, ignore_errors=True)
    return out_dir


def read_meta(area_dir: Path) -> dict | None:
    try:
        return json.loads((area_dir / META).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def read(area_dir: Path) -> Package:
    meta = read_meta(area_dir)
    if meta is None:
        raise PackageError(f"{area_dir} has no graph yet; run python -m routing.build")
    if meta.get("schema") != model.SCHEMA:
        raise PackageError(
            f"{area_dir} holds schema {meta.get('schema')}, this code reads {model.SCHEMA}; rebuild it"
        )
    with np.load(area_dir / GRAPH, allow_pickle=False) as data:
        arrays = {name: data[name] for name in ARRAYS}
    return Package(meta=meta, **arrays)


class LazyPackage:
    """A package whose arrays are read from graph.npz one at a time, when
    asked, and not kept: routing.compile merges dozens of tiles and needs
    one field of all of them at a time, never everything at once."""

    def __init__(self, area_dir: Path):
        meta = read_meta(area_dir)
        if meta is None:
            raise PackageError(f"{area_dir} has no graph yet; run python -m routing.build")
        if meta.get("schema") != model.SCHEMA:
            raise PackageError(
                f"{area_dir} holds schema {meta.get('schema')}, this code reads {model.SCHEMA}; rebuild it"
            )
        self.meta = meta
        self._npz = np.load(area_dir / GRAPH, allow_pickle=False)
        self.node_count = len(self._npz["node_kind"])
        self.edge_count = len(self._npz["edge_kind"])
        self.geom_count = len(self._npz["geom_ele"])

    @property
    def epsg(self) -> int:
        return int(self.meta["epsg"])

    def __getattr__(self, name: str) -> np.ndarray:
        if name.startswith("_") or name not in ARRAYS:
            raise AttributeError(name)
        return self._npz[name]
