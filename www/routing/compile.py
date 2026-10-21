"""Compile graph packages into one routable graph on disk.

    python -m routing.compile --all            every area and region with packages
    python -m routing.compile --area caucasus  one region: its tiles merged

A region's tiles are built separately (routing.build) and overlap in a strip
along their seams. Here they become one graph:

* nodes merge: OSM nodes by id, generated ones by position - a region's tiles
  share a CRS and a lattice anchored to it, so the lattice (and the DEM cells
  thalwegs run through) lands on the same points on both sides of a seam;
* edges merge: the copy of an edge the neighbouring tile also built is dropped;
* nodes are renumbered cell by cell (SNAP_CELL_M squares), which is both the
  index waypoints are snapped with and what keeps a search's reads close
  together on disk;
* arcs (one per walkable direction) are laid out as CSR with every cost the
  search needs next to the head node: time, weight, the kind, and the
  backpack folded into two numbers (pack_t, pack_w: linear in the load, with
  the config's pack coefficients - change those, compile again);
* passes, on a fraction of a per cent of the arcs, are kept apart (pass_arc,
  pass_need, pass_over) and flagged in arc_kind; ids are 32-bit, node lon/lat
  not stored - about 40 bytes an arc in all.

Every array is a plain .npy file, opened with ``np.load(mmap_mode="r")``: the
server keeps only the pages a search touches (routing.engine.disk), which is
how the whole of the Caucasus answers from a 2 GB machine.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from routing import config as routing_config  # noqa: E402
from routing import model, pack, package  # noqa: E402

log = logging.getLogger("routing.compile")

FORMAT = 6  # 6: the backpack folded into arc_pack_t / arc_pack_w, passes sparse, narrower ids
# arc_kind: the edge kind in the low bits, this one set where the arc goes
# over a pass or needs one (looked up in pass_arc / pass_need / pass_over)
ARC_HAS_PASS = 0x80
SNAP_CELL_M = 500.0
# a component this big (nodes, or share of the graph) is one a route can use
MAIN_MIN_NODES = 2000
MAIN_MIN_SHARE = 0.002
GRAPHS_DIR = "graphs"

EDGE_FIELDS = ("time_fwd", "time_bwd", "weight_fwd", "weight_bwd", "climb_fwd", "climb_bwd",
               "descent_fwd", "descent_bwd", "rough_fwd", "rough_bwd", "steep_fwd", "steep_bwd")


def graphs_dir(cfg: routing_config.Config) -> Path:
    return cfg.routing_dir / GRAPHS_DIR


def members(cfg: routing_config.Config, name: str) -> list[str]:
    """The packages a graph is made of: a region's tiles, or the area itself."""
    if cfg.region(name) is not None:
        return [t.name for t in cfg.tiles_of(name)]
    return [cfg.area(name).name]


def _node_keys(pkg: package.Package) -> np.ndarray:
    """OSM nodes by -id, the rest by position (centimetres in the shared CRS)."""
    x = np.round(pkg.node_x * 100).astype(np.int64)
    y = np.round(pkg.node_y * 100).astype(np.int64)
    pos = (x + (1 << 31)) * (1 << 32) + y
    return np.where(pkg.node_kind == model.OSM, -pkg.node_osm, pos)


def _stamp(cfg: routing_config.Config, names: list[str]) -> dict:
    """What a compiled graph is made of: its packages (each by the build's
    fingerprint and time), the format and the folded pack coefficients."""
    sources = {}
    for n in names:
        meta = package.read_meta(cfg.area_dir(n)) or {}
        sources[n] = f"{meta.get('fingerprint')}@{meta.get('built_at')}"
    return {"format": FORMAT, "schema": model.SCHEMA, "pack": dataclasses.asdict(cfg.pack), "sources": sources}


def is_current(cfg: routing_config.Config, name: str) -> bool:
    """Whether graphs/<name>/ was compiled from these very packages, as this
    code would compile them now."""
    try:
        meta = json.loads((graphs_dir(cfg) / name / "meta.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return False
    stamp = _stamp(cfg, members(cfg, name))
    return all(meta.get(k) == v for k, v in stamp.items())


def compile_graph(cfg: routing_config.Config, name: str) -> Path:
    """Merge *name*'s packages into graphs/<name>/. Frugal with memory - the
    Caucasus is tens of millions of edges: packages are read a field at a
    time (package.LazyPackage), ids are 32-bit, and every arc array is written
    as soon as it is made."""
    started = time.monotonic()
    names = members(cfg, name)
    pkgs, epsg = [], None
    for n in names:
        pkg = package.LazyPackage(cfg.area_dir(n))
        if epsg is not None and pkg.epsg != epsg:
            raise RuntimeError(f"{n} is in EPSG:{pkg.epsg}, the others in EPSG:{epsg}; rebuild the region")
        epsg = pkg.epsg
        pkgs.append(pkg)
    log.info("%s: merging %s package(s)", name, len(pkgs))
    node_off = np.cumsum([0] + [p.node_count for p in pkgs])
    geom_base = np.cumsum([0] + [p.geom_count for p in pkgs])

    def cat(attr: str, dtype=None) -> np.ndarray:
        return np.concatenate([np.asarray(getattr(p, attr), dtype=dtype) for p in pkgs])

    def per_edge(fn, dtype=None) -> np.ndarray:
        return np.concatenate([np.asarray(fn(i, p), dtype=dtype) for i, p in enumerate(pkgs)])

    # ------------------------------------------------------------- nodes
    # Of a node several tiles have, the copy of a tile whose DEM it is on: an
    # OSM node past the DEM of a tile its way reaches into reads height 0
    # there (no data). Sorted that way, the first of each key is that copy.
    keys = np.concatenate([_node_keys(p) for p in pkgs])
    off_dem = cat("node_ele") == 0
    perm = np.lexsort((off_dem, keys))
    sorted_keys = keys[perm]
    del keys
    head_of = np.r_[True, sorted_keys[1:] != sorted_keys[:-1]]
    del sorted_keys
    first = perm[head_of]
    inverse = np.empty(len(perm), dtype=np.int64)
    inverse[perm] = np.cumsum(head_of) - 1
    del perm, head_of
    N = len(first)
    if N >= 2 ** 31:
        raise RuntimeError(f"{name}: too many nodes for 32-bit ids")
    nx_, ny_ = cat("node_x")[first], cat("node_y")[first]
    ele = cat("node_ele")[first]
    kind = cat("node_kind")[first]
    del first

    # renumber by snapping cell: row-major cells, the index of the snapper
    x0, y0 = float(nx_.min()), float(ny_.min())
    ncx = int((nx_.max() - x0) // SNAP_CELL_M) + 1
    ncy = int((ny_.max() - y0) // SNAP_CELL_M) + 1
    cell = ((ny_ - y0) // SNAP_CELL_M).astype(np.int64) * ncx + ((nx_ - x0) // SNAP_CELL_M).astype(np.int64)
    norder = np.argsort(cell, kind="stable")
    rank = np.empty(N, dtype=np.int32)
    rank[norder] = np.arange(N, dtype=np.int32)
    cell_sorted = cell[norder]
    del cell
    to_global = rank[inverse]  # package-local (concatenated) node -> final id
    del inverse, rank

    # ------------------------------------------------------------ passes
    # one entry per OSM pass, whichever tiles saw it
    pass_keys = cat("pass_osm")
    pass_names = sum((list(p.meta.get("pass_names") or [""] * len(p.pass_osm)) for p in pkgs), [])
    pass_off = np.cumsum([0] + [len(p.pass_osm) for p in pkgs])
    puniq, pfirst, pinv = np.unique(pass_keys, return_index=True, return_inverse=True)
    # a -1 looked up too (np.where takes both sides): one past the end
    pinv = np.append(pinv, -1).astype(np.int32)

    def global_pass(attr: str) -> np.ndarray:
        if not len(puniq):
            return np.full(sum(p.edge_count for p in pkgs), -1, dtype=np.int32)
        return per_edge(lambda i, p: (lambda a: np.where(a >= 0, pinv[np.maximum(a, 0) + pass_off[i]], -1))(
            np.asarray(getattr(p, attr))), np.int32)

    # ------------------------------------------------------------- edges
    # one copy of each edge: oriented low -> high node, the first of each
    # (u, v, kind) - the copy costed on its own tile's DEM if there is one
    u = per_edge(lambda i, p: to_global[np.asarray(p.edge_u) + node_off[i]])
    v = per_edge(lambda i, p: to_global[np.asarray(p.edge_v) + node_off[i]])
    edge_off = per_edge(lambda i, p: off_dem[np.asarray(p.edge_u) + node_off[i]]
                        | off_dem[np.asarray(p.edge_v) + node_off[i]])
    to_global = off_dem = None  # (the closures above are done with them)
    swap = u > v
    lo, hi = np.minimum(u, v), np.maximum(u, v)
    del u, v
    ekind = cat("edge_kind")
    kept = np.flatnonzero(lo != hi)
    key = (lo[kept].astype(np.int64) * N + hi[kept]) * 8 + ekind[kept]
    eperm = np.lexsort((edge_off[kept], key))
    del edge_off
    key = key[eperm]
    firsts = np.r_[True, key[1:] != key[:-1]]
    del key
    ordered = kept[eperm]
    representatives = ordered[firsts]

    # An OSM edge in the overlap can occur in one tile which sees the pass
    # point and in another whose OSM margin sees the way but not the point.
    # Keep pass metadata from any duplicate, not only from the copy selected
    # for its on-DEM cost. Otherwise the compiled trail can lose its category.
    groups = np.cumsum(firsts) - 1
    all_epass, all_ecross = global_pass("edge_pass"), global_pass("edge_cross")

    def duplicate_pass(values: np.ndarray) -> np.ndarray:
        merged = np.full(len(representatives), -1, dtype=np.int32)
        valid = values[ordered] >= 0
        vg = groups[valid]
        vv = values[ordered[valid]]
        if len(vg):
            first_valid = np.r_[True, vg[1:] != vg[:-1]]
            merged[vg[first_valid]] = vv[first_valid]
        out = np.full(len(values), -1, dtype=np.int32)
        out[representatives] = merged
        return out

    merged_epass = duplicate_pass(all_epass)
    merged_ecross = duplicate_pass(all_ecross)
    del all_epass, all_ecross, groups, ordered
    sel = np.sort(representatives)
    del representatives
    total = len(swap)
    del kept, eperm, firsts
    # an edge closed both ways is no edge
    live = np.isfinite(cat("time_fwd", np.float32)[sel]) | np.isfinite(cat("time_bwd", np.float32)[sel])
    closed = int((~live).sum())
    sel = sel[live]
    del live
    u, v, swap, ekind = lo[sel], hi[sel], swap[sel], ekind[sel]
    del lo, hi
    # and a node no edge reaches is no node: a lattice point in a lake, a
    # line node every edge of which a barrier took
    used = np.zeros(N, dtype=bool)
    used[u] = used[v] = True
    new_id = (np.cumsum(used) - 1).astype(np.int32)
    u, v = new_id[u], new_id[v]
    del new_id
    log.info("%s: %s nodes (%s merged, %s with no edge dropped), %s edges (%s duplicates, %s closed both "
             "ways dropped)", name, int(used.sum()), int(node_off[-1]) - N, N - int(used.sum()), len(sel),
             total - len(sel) - closed, closed)
    N = int(used.sum())
    cell_start = np.searchsorted(cell_sorted[used], np.arange(ncx * ncy + 1)).astype(np.int64)
    del cell_sorted
    E = len(sel)
    if E * 2 >= 2 ** 31:
        raise RuntimeError(f"{name}: too many edges for 32-bit ids")
    epass, ecross = merged_epass[sel], merged_ecross[sel]
    del merged_epass, merged_ecross

    def both(fwd: str, bwd: str) -> tuple[np.ndarray, np.ndarray]:
        """A per-direction field pair of the kept edges, low -> high node first."""
        a, b = cat(fwd, np.float32)[sel], cat(bwd, np.float32)[sel]
        return np.where(swap, b, a), np.where(swap, a, b)

    # ------------------------------------------------------------- write
    out = graphs_dir(cfg) / name
    staging = out.with_name(f".{name}.new")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    def save(key: str, arr: np.ndarray) -> None:
        np.save(staging / f"{key}.npy", np.ascontiguousarray(arr))

    # ------------------------------------------------------------- arcs
    t_fwd, t_bwd = both("time_fwd", "time_bwd")
    fwd, bwd = np.isfinite(t_fwd), np.isfinite(t_bwd)
    eid = np.concatenate([np.flatnonzero(fwd), np.flatnonzero(bwd)]).astype(np.int32)
    dirn = np.concatenate([np.zeros(int(fwd.sum()), np.bool_), np.ones(int(bwd.sum()), np.bool_)])
    tail = np.concatenate([u[fwd], v[bwd]])
    order = np.argsort(tail, kind="stable")
    tail = tail[order]
    eid, dirn = eid[order], dirn[order]
    del order, fwd, bwd
    A = len(eid)
    indptr = np.searchsorted(tail, np.arange(N + 1)).astype(np.int64)
    head = np.where(dirn, u[eid], v[eid])
    save("arc_indptr", indptr)
    save("arc_head", head)
    save("arc_ref", eid * 2 + dirn.astype(np.int32))

    def arcs_of(pair: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
        a, b = pair
        return np.where(dirn, b[eid], a[eid]).astype(np.float32)

    # The backpack is linear in the load (routing.pack), and its coefficients
    # are the service's config, not the request's: folded in here, per 10 kg
    #   time(kg) = time + kg/10 x pack_t,   weight(kg) = weight + kg/10 x pack_w
    # - two numbers an arc instead of the five the formula reads.
    a_time = arcs_of((t_fwd, t_bwd))
    del t_fwd, t_bwd
    save("arc_time", a_time)
    a_weight = arcs_of(both("weight_fwd", "weight_bwd"))
    save("arc_weight", a_weight)
    pack_t = pack.per_10kg_time(a_time, arcs_of(both("climb_fwd", "climb_bwd")),
                                arcs_of(both("descent_fwd", "descent_bwd")),
                                arcs_of(both("rough_fwd", "rough_bwd")), cfg.pack).astype(np.float32)
    save("arc_pack_t", pack_t)
    a_extra = cat("edge_extra", np.float32)[sel][eid]
    pack_w = pack.per_10kg_weight(a_weight, a_time, pack_t, a_extra,
                                  arcs_of(both("steep_fwd", "steep_bwd")), cfg.pack).astype(np.float32)
    save("arc_pack_w", pack_w)
    del a_time, a_weight, pack_t, a_extra, pack_w

    # passes: on a fraction of a per cent of the arcs, so kept apart
    a_need, a_over = epass[eid], ecross[eid]
    with_pass = np.flatnonzero((a_need >= 0) | (a_over >= 0))
    arc_kind = ekind[eid].astype(np.uint8)
    arc_kind[with_pass] |= ARC_HAS_PASS
    save("arc_kind", arc_kind)
    save("pass_arc", with_pass.astype(np.int64))
    save("pass_need", a_need[with_pass])
    save("pass_over", a_over[with_pass])
    del a_need, a_over, with_pass, arc_kind, epass, ecross

    # node flags: usable (in and out), on a trail, in a big component
    out_deg = np.diff(indptr)
    in_deg = np.bincount(head, minlength=N)
    osm_arc = ekind[eid] == model.OSM
    osm_deg = np.bincount(tail[osm_arc], minlength=N) + np.bincount(head[osm_arc], minlength=N)
    flags = ((out_deg > 0) & (in_deg > 0)).astype(np.uint8) | ((osm_deg > 0).astype(np.uint8) << 1)
    del out_deg, in_deg, osm_arc, osm_deg, tail
    eid = dirn = None  # (arcs_of is done with them)
    # nodes of a big strongly connected component: what a waypoint snaps to
    # first, so a click on a ledge the graph cannot leave is not a dead end
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import connected_components

    adj = csr_matrix((np.ones(A, np.int8), head, indptr), shape=(N, N))
    ncomp, comp = connected_components(adj, directed=True, connection="strong")
    del adj, head
    sizes = np.bincount(comp, minlength=ncomp)
    big = sizes[comp] >= max(MAIN_MIN_NODES, int(MAIN_MIN_SHARE * N))
    flags |= big.astype(np.uint8) << 2
    log.info("%s: %s strongly connected components, the largest %.1f%% of the nodes", name, ncomp,
             100 * sizes.max() / max(N, 1))
    del comp, big

    save("node_x", nx_[norder][used].astype(np.float32))
    save("node_y", ny_[norder][used].astype(np.float32))
    save("node_ele", ele[norder][used].astype(np.float32))
    save("node_kind", kind[norder][used])
    save("node_flags", flags)
    save("cell_start", cell_start)
    del nx_, ny_, ele, kind, flags
    save("pass_osm", puniq)
    save("pass_grade", cat("pass_grade")[pfirst].astype(np.uint8))
    save("pass_lon", cat("pass_lon")[pfirst])
    save("pass_lat", cat("pass_lat")[pfirst])
    save("edge_u", u)
    save("edge_v", v)
    save("edge_len", cat("edge_len", np.float32)[sel])
    save("edge_kind", ekind)
    save("edge_terrain", cat("edge_terrain")[sel])
    del u, v

    # geometry: polylines only, stored low -> high node like their edge; a
    # straight edge is drawn between its nodes
    counts = per_edge(lambda i, p: np.diff(np.asarray(p.geom_off)), np.int64)[sel]
    gstart = per_edge(lambda i, p: np.asarray(p.geom_off)[:-1] + geom_base[i], np.int64)[sel]
    poly = counts > 2
    pcounts = np.where(poly, counts, 0)
    geom_off = np.concatenate([[0], np.cumsum(pcounts)])
    if geom_off[-1] >= 2 ** 31:
        raise RuntimeError(f"{name}: too much geometry for 32-bit offsets")
    save("geom_off", geom_off.astype(np.int32))
    starts, lens, rev = gstart[poly], counts[poly], swap[poly]
    within = np.arange(int(lens.sum())) - np.repeat(np.cumsum(lens) - lens, lens)
    take = np.repeat(starts, lens) + np.where(np.repeat(rev, lens), np.repeat(lens, lens) - 1 - within, within)
    del counts, gstart, poly, pcounts, starts, lens, rev, within
    for key, dtype in (("geom_lon", None), ("geom_lat", None), ("geom_ele", np.float32)):
        save(key, cat(key, dtype)[take])
    del take

    import shapely

    coverage = shapely.union_all([shapely.box(*cfg.area(n).bbox) for n in names])
    meta = {
        "format": FORMAT, "schema": model.SCHEMA, "name": name, "epsg": epsg,
        "region": cfg.region(name) is not None, "members": names,
        "bbox": list(coverage.bounds), "coverage": shapely.geometry.mapping(coverage),
        "snap": {"x0": x0, "y0": y0, "cell": SNAP_CELL_M, "ncx": ncx, "ncy": ncy},
        "nodes": int(N), "edges": int(E), "arcs": int(A),
        "pass_names": [pass_names[i] for i in pfirst],
        # what arc_pack_* were folded with, and the packages it was made of:
        # either changing needs a recompile (is_current)
        **_stamp(cfg, names),
        "edges_by_kind": {model.KINDS[k]: int(c) for k, c in enumerate(np.bincount(ekind, minlength=len(model.KINDS)))},
        "built_at": max(p.meta.get("built_at", "") for p in pkgs),
        "compiled_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (staging / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    retired = out.with_name(f".{name}.old")
    shutil.rmtree(retired, ignore_errors=True)
    if out.exists():
        out.rename(retired)
    staging.rename(out)
    shutil.rmtree(retired, ignore_errors=True)
    size = sum(f.stat().st_size for f in out.iterdir()) / 2**20
    log.info("%s: %s nodes, %s edges, %s arcs, %.0f MB on disk, in %.0fs", name, N, E, A, size,
             time.monotonic() - started)
    return out


def compilable(cfg: routing_config.Config) -> list[str]:
    """Plain areas and regions with every member built."""
    out = []
    for area in cfg.areas:
        if area.region is None and package.read_meta(cfg.area_dir(area.name)):
            out.append(area.name)
    for region in cfg.regions:
        tiles = cfg.tiles_of(region.name)
        built = [t for t in tiles if package.read_meta(cfg.area_dir(t.name))]
        if tiles and len(built) == len(tiles):
            out.append(region.name)
        elif tiles:
            log.warning("%s: %s of %s tiles built, not compiled", region.name, len(built), len(tiles))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path)
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--area", action="append", help="area or region (repeatable)")
    which.add_argument("--all", action="store_true")
    parser.add_argument("--force", action="store_true", help="compile even a graph that is current")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = routing_config.load(args.config)
    for name in (compilable(cfg) if args.all else args.area):
        if not args.force and is_current(cfg, name):
            log.info("%s: graph is current", name)
            continue
        compile_graph(cfg, name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
