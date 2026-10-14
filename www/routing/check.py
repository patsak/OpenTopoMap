"""Check a built graph: ``python -m routing.check --area NAME``.

Reads the area's package (and, for the routes, the compiled graph holding
it: the area's own, or its region's) and reports what would make routes wrong
rather than merely slow:

* connectivity: strongly connected components, and the islands outside the
  largest (written to --islands as GeoJSON to look at);
* barriers: an off-trail edge crossing water, a cliff or a canyon (needs the
  build's debug.gpkg) - the one hard failure, exit status 1;
* passes with fewer than two descents into the valleys;
* times: edges closed both ways, NaN, speeds by kind and terrain;
* random routes between lattice nodes: found or not, and how much off trail.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from scipy.sparse import coo_matrix  # noqa: E402
from scipy.sparse.csgraph import connected_components  # noqa: E402

from routing import compile as routing_compile  # noqa: E402
from routing import config, model, package  # noqa: E402
from routing.engine.base import Options, RouteError  # noqa: E402
from routing.package import PackageError  # noqa: E402

log = logging.getLogger("routing.check")


def barrier_violations(pkg: package.Package, gpkg: Path) -> tuple[int, dict[str, int]] | None:
    import shapely
    from osgeo import ogr

    if not gpkg.is_file():
        return None
    source = ogr.Open(str(gpkg))
    layer = source.GetLayerByName("barriers")
    geoms, labels = [], []
    for feature in layer:
        geom = feature.GetGeometryRef()
        # the DEM's steep patches are crossed on purpose by fall lines and in
        # pass zones (graph.SOFT_BARRIERS): not a violation
        if geom is not None and feature["label"] != "steep":
            geoms.append(shapely.from_wkb(bytes(geom.ExportToWkb())))
            labels.append(feature["label"])
    del source
    if not geoms:
        return 0, {}
    offroad = np.flatnonzero(~np.isin(pkg.edge_kind, (model.OSM, model.FORD)))
    counts = np.diff(pkg.geom_off)[offroad]
    starts = pkg.geom_off[offroad]
    take = np.concatenate([np.arange(s, s + c) for s, c in zip(starts, counts)])
    xy = np.column_stack([pkg.geom_lon[take], pkg.geom_lat[take]])
    lines = shapely.linestrings(xy, indices=np.repeat(np.arange(len(offroad)), counts))
    tree = shapely.STRtree(np.asarray(geoms, dtype=object))
    hit_edge, hit_barrier = tree.query(lines, predicate="intersects")
    # touching the outline (a fall line through the corner of a cliff
    # polygon traced from the raster) is not crossing it; after the trip to
    # lon/lat a touch can overlap by a hair, so anything under ~0.2 m is one
    overlap = shapely.length(shapely.intersection(lines[hit_edge], np.asarray(geoms, dtype=object)[hit_barrier]))
    inside = overlap > 2e-6
    hit_edge, hit_barrier = hit_edge[inside], hit_barrier[inside]
    by_label: dict[str, int] = {}
    for b in np.unique(hit_barrier):
        by_label[labels[b]] = by_label.get(labels[b], 0) + int((hit_barrier == b).sum())
    return len(np.unique(hit_edge)), by_label


def passes_report(gpkg: Path) -> tuple[int, int, list[str]] | None:
    from osgeo import ogr

    if not gpkg.is_file():
        return None
    source = ogr.Open(str(gpkg))
    total, short, names = 0, 0, []
    for feature in source.GetLayerByName("passes"):
        total += 1
        if feature["descents"] < 2:
            short += 1
            names.append(f"{feature['osm_id']} {feature['name'] or ''}".strip())
    del source
    return total, short, names


def components(pkg: package.Package) -> tuple[np.ndarray, np.ndarray]:
    """Strongly connected components over the edges walkable each way: the
    label of every node, and which nodes have an edge at all."""
    fwd = np.isfinite(pkg.time_fwd)
    bwd = np.isfinite(pkg.time_bwd)
    src = np.concatenate([pkg.edge_u[fwd], pkg.edge_v[bwd]])
    dst = np.concatenate([pkg.edge_v[fwd], pkg.edge_u[bwd]])
    n = pkg.node_count
    adj = coo_matrix((np.ones(len(src), np.int8), (src, dst)), shape=(n, n)).tocsr()
    _, labels = connected_components(adj, directed=True, connection="strong")
    linked = np.zeros(n, dtype=bool)
    linked[pkg.edge_u] = linked[pkg.edge_v] = True
    return labels, linked


def run(cfg: config.Config, area: str, pairs: int, islands_out: Path | None, seed: int) -> int:
    area_dir = cfg.area_dir(area)
    pkg = package.read(area_dir)
    print(f"area {area}: built {pkg.meta.get('built_at')}, {pkg.node_count} nodes, {pkg.edge_count} edges")
    for name, count in pkg.meta.get("edges", {}).items():
        print(f"  {name:9s} {count}")
    failures = 0

    # ---------------------------------------------------------- connectivity
    labels, linked = components(pkg)
    sizes = np.bincount(labels[linked]) if linked.any() else np.zeros(1, np.int64)
    by_size = np.argsort(-sizes)
    by_size = by_size[sizes[by_size] > 0]
    in_main = linked & (labels == by_size[0]) if len(by_size) else np.zeros(pkg.node_count, bool)
    print(f"\nconnectivity: {len(by_size)} strongly connected components; the largest holds "
          f"{int(in_main.sum())} of {int(linked.sum())} linked nodes "
          f"({100 * in_main.sum() / max(1, linked.sum()):.1f}%)")
    osm_nodes = np.flatnonzero(pkg.node_kind == model.OSM)
    osm_in_main = in_main[osm_nodes]
    print(f"  OSM nodes in it: {100 * osm_in_main.mean() if len(osm_nodes) else 0:.1f}% "
          "(the rest: trails cut off by a barrier, or outside the area)")
    if islands_out is not None:
        features = []
        for comp in by_size[1:500]:
            idx = np.flatnonzero(linked & (labels == comp))
            features.append({"type": "Feature", "properties": {"nodes": len(idx)},
                             "geometry": {"type": "MultiPoint", "coordinates": np.column_stack(
                                 [pkg.node_lon[idx], pkg.node_lat[idx]]).round(6).tolist()}})
        islands_out.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
        print(f"  islands written to {islands_out}")

    # --------------------------------------------------------------- barriers
    gpkg = area_dir / package.DEBUG
    violations = barrier_violations(pkg, gpkg)
    if violations is None:
        print("\nbarriers: no debug.gpkg, not checked")
    else:
        bad, by_label = violations
        print(f"\nbarriers: {bad} off-trail edges cross one" + (f" {by_label}" if by_label else ""))
        failures += bad > 0

    passes = passes_report(gpkg)
    if passes is not None:
        total, short, names = passes
        print(f"\npasses: {total}, with fewer than two descents: {short}")
        for name in names[:20]:
            print(f"  {name}")

    # ------------------------------------------------------------------ times
    closed = ~np.isfinite(pkg.time_fwd) & ~np.isfinite(pkg.time_bwd)
    nan = np.isnan(pkg.time_fwd) | np.isnan(pkg.time_bwd)
    print(f"\ntimes: {int(closed.sum())} edges closed both ways, {int(nan.sum())} NaN")
    failures += int(nan.sum()) > 0
    ok = np.isfinite(pkg.time_fwd) & (pkg.time_fwd > 0)
    kmh = np.where(ok, 3.6 * pkg.edge_len / np.where(ok, pkg.time_fwd, 1), np.nan)
    print("  median speed, km/h, by kind / terrain:")
    for k, kind in enumerate(model.KINDS):
        sel = (pkg.edge_kind == k) & ok
        if not sel.any():
            continue
        parts = []
        for t, terrain in enumerate(model.TERRAINS):
            st = sel & (pkg.edge_terrain == t)
            if st.sum() >= 20:
                parts.append(f"{terrain} {np.nanmedian(kmh[st]):.1f}")
        print(f"  {kind:9s} {np.nanmedian(kmh[sel]):.1f}  ({', '.join(parts)})")

    # ----------------------------------------------------------------- routes
    lattice = np.flatnonzero(in_main & (pkg.node_kind == model.LATTICE))
    engine = None
    if pairs and len(lattice) >= 2:
        from routing.graphstore import engine_factory

        name = cfg.area(area).region or area
        try:
            engine = engine_factory()(routing_compile.graphs_dir(cfg) / name)
        except (FileNotFoundError, PackageError) as exc:
            print(f"\nroutes: graph {name} not compiled ({exc}); run python -m routing.compile")
    if engine is not None:
        graph = engine.graph
        rng = np.random.default_rng(seed)
        found, offroad, kmh_all = 0, [], []
        for _ in range(pairs):
            a, b = rng.choice(lattice, 2, replace=False)
            points = [(float(pkg.node_lon[a]), float(pkg.node_lat[a])),
                      (float(pkg.node_lon[b]), float(pkg.node_lat[b]))]
            try:
                path = engine.route(points, Options())
            except RouteError:
                continue
            found += 1
            edges = [i for leg in path.legs for i, _ in leg.edges]
            if not edges:
                continue
            kinds = np.asarray(graph.edge_kind[edges])
            lengths = np.asarray(graph.edge_len[edges], np.float64)
            length = lengths.sum()
            off = lengths[kinds != model.OSM].sum()
            step_time = engine.edge_time(path, Options())
            seconds = sum(step_time(i, f) for leg in path.legs for i, f in leg.edges)
            offroad.append(off / length if length else 0)
            kmh_all.append(3.6 * length / seconds if seconds else 0)
        print(f"\nroutes: {found}/{pairs} random lattice pairs routed; off trail "
              f"{100 * np.mean(offroad):.0f}% of the distance, {np.mean(kmh_all):.1f} km/h on average"
              if found else f"\nroutes: none of {pairs} random pairs routed")
        failures += found < pairs
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--area", required=True)
    parser.add_argument("--pairs", type=int, default=20, help="random routes to try (default 20)")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--islands", type=Path, help="write the components outside the largest here")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    cfg = config.load(args.config)
    return run(cfg, args.area, args.pairs, args.islands, args.seed)


if __name__ == "__main__":
    sys.exit(main())
