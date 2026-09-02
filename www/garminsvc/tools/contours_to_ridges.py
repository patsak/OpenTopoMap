#!/usr/bin/env python3
"""Build ridge lines (хребтовка) for mid-zoom sparse contours.

OsmAnd approach (see OsmAnd-resources default/topo.render.xml):
  ridges are NOT generated from DEM. The app draws OSM ways tagged
  natural=ridge / natural=arete (manually mapped crest lines).

This script extracts those ways from an OSM PBF and writes a lightweight
OSM XML for the contours-hike overlay (resolutions 20–22).

Usage:
  .venv/bin/python tools/contours_to_ridges.py \\
      --osm-pbf data/north-caucasus-fed-district-260805.osm.pbf \\
      --output data/ridges-demk38/ridges.osm

Optional DEM mode (--hgt-dir) remains for experiments; prefer --osm-pbf.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

import numpy as np
from scipy import ndimage
from shapely.geometry import LineString

import npyosmium as osmium


HGT_RE = re.compile(r"([NS])(\d{2})([EW])(\d{3})\.hgt$", re.I)
VOID = -32768
RIDGE_TAGS = frozenset({"ridge", "arete"})


# ---------------------------------------------------------------------------
# OSM extraction (OsmAnd-compatible)
# ---------------------------------------------------------------------------

class RidgeExtractor(osmium.SimpleHandler):
    def __init__(self, min_nodes: int = 2):
        super().__init__()
        self.min_nodes = min_nodes
        self.lines: list[tuple[list[tuple[float, float]], dict[str, str]]] = []
        self.skipped_no_loc = 0

    def way(self, w):
        nat = w.tags.get("natural")
        if nat not in RIDGE_TAGS:
            return
        coords: list[tuple[float, float]] = []
        for n in w.nodes:
            if not n.location.valid():
                self.skipped_no_loc += 1
                return
            coords.append((n.location.lon, n.location.lat))
        if len(coords) < self.min_nodes:
            return
        tags = {"contour": "ridge", "natural": nat}
        # Moraine crests get their own symbol in the main map, so the overlay has
        # to know about them and skip drawing a generic ridge line on top.
        if w.tags.get("geological") == "moraine":
            tags["geological"] = "moraine"
        name = w.tags.get("name")
        if name:
            tags["name"] = name
        self.lines.append((coords, tags))


def extract_osm_ridges(pbf: Path, min_nodes: int = 2) -> list[tuple[LineString, dict[str, str]]]:
    h = RidgeExtractor(min_nodes=min_nodes)
    # locations=True resolves node coords for ways
    h.apply_file(str(pbf), locations=True, idx="flex_mem")
    out: list[tuple[LineString, dict[str, str]]] = []
    for coords, tags in h.lines:
        ls = LineString(coords)
        if ls.is_empty or ls.length <= 0:
            continue
        out.append((ls, tags))
    print(f"OSM ridges: {len(out)} ways (skipped incomplete: {h.skipped_no_loc})")
    return out


def write_osm(path: Path, items: list[tuple[LineString, dict[str, str]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    node_id = 0
    way_id = 0
    with path.open("w", encoding="utf-8") as f:
        f.write("<?xml version='1.0' encoding='UTF-8'?>\n")
        f.write("<osm version='0.6' generator='contours_to_ridges'>\n")
        for ls, tags in items:
            refs = []
            for lon, lat in ls.coords:
                node_id -= 1
                refs.append(node_id)
                f.write(f'  <node id="{node_id}" lat="{lat:.7f}" lon="{lon:.7f}" />\n')
            way_id -= 1
            f.write(f'  <way id="{way_id}">\n')
            for ref in refs:
                f.write(f'    <nd ref="{ref}" />\n')
            for k, v in tags.items():
                # escape XML special chars in names
                vv = (
                    v.replace("&", "&amp;")
                    .replace('"', "&quot;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                f.write(f'    <tag k="{k}" v="{vv}" />\n')
            f.write("  </way>\n")
        f.write("</osm>\n")


# ---------------------------------------------------------------------------
# Experimental DEM mode (kept for comparison; not used by OsmAnd)
# ---------------------------------------------------------------------------

def parse_hgt_name(path: Path) -> tuple[float, float]:
    m = HGT_RE.search(path.name)
    if not m:
        raise ValueError(f"Unrecognized HGT name: {path.name}")
    lat = int(m.group(2)) * (1 if m.group(1).upper() == "N" else -1)
    lon = int(m.group(4)) * (1 if m.group(3).upper() == "E" else -1)
    return float(lat), float(lon)


def read_hgt(path: Path) -> np.ndarray:
    raw = path.read_bytes()
    n = int(math.sqrt(len(raw) / 2))
    if n * n * 2 != len(raw):
        raise ValueError(f"Bad HGT size {len(raw)} for {path}")
    dem = np.frombuffer(raw, dtype=">i2").reshape(n, n).astype(np.float64)
    dem[dem == VOID] = np.nan
    return dem


def ridge_mask_dem(dem: np.ndarray, smooth_sigma: float, min_drop: float, min_axes: int, relief_std: float) -> np.ndarray:
    valid = np.isfinite(dem)
    if valid.sum() < 100:
        return np.zeros(dem.shape, dtype=bool)
    filled = dem.copy()
    if not valid.all():
        ind = ndimage.distance_transform_edt(~valid, return_distances=False, return_indices=True)
        filled = dem[tuple(ind)]
    s = ndimage.gaussian_filter(filled, sigma=smooth_sigma)

    def axis_ridge(a, b):
        return (s >= a + min_drop) & (s >= b + min_drop)

    ns = axis_ridge(np.roll(s, 1, 0), np.roll(s, -1, 0))
    ew = axis_ridge(np.roll(s, 1, 1), np.roll(s, -1, 1))
    d1 = axis_ridge(np.roll(np.roll(s, 1, 0), 1, 1), np.roll(np.roll(s, -1, 0), -1, 1))
    d2 = axis_ridge(np.roll(np.roll(s, 1, 0), -1, 1), np.roll(np.roll(s, -1, 0), 1, 1))
    score = ns.astype(np.uint8) + ew + d1 + d2
    mask = (score >= min_axes) & valid
    mean = ndimage.uniform_filter(s, size=15)
    mean2 = ndimage.uniform_filter(s * s, size=15)
    std = np.sqrt(np.maximum(mean2 - mean * mean, 0.0))
    mask &= std >= relief_std
    mask[:2, :] = mask[-2:, :] = mask[:, :2] = mask[:, -2:] = False
    return mask


def thin_connected(mask: np.ndarray) -> np.ndarray:
    if not mask.any():
        return mask
    m = ndimage.binary_closing(mask, structure=np.ones((3, 3)), iterations=1)
    # keep thick-ish crest (axes=1 yields connected ribbons); light erode only
    skel = ndimage.binary_erosion(m, iterations=1)
    return skel if skel.any() else m


def pixel_to_lonlat(r, c, lat0, lon0, n):
    return lon0 + c / (n - 1), lat0 + 1.0 - r / (n - 1)


def trace_skeleton(skel, lat0, lon0):
    n = skel.shape[0]
    visited = np.zeros_like(skel, dtype=bool)
    coords = np.argwhere(skel)
    nbrs = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]

    def neigh(r, c, only_unvisited=True):
        for dr, dc in nbrs:
            rr, cc = r + dr, c + dc
            if 0 <= rr < n and 0 <= cc < n and skel[rr, cc]:
                if only_unvisited and visited[rr, cc]:
                    continue
                yield rr, cc

    def degree(r, c):
        return sum(1 for _ in neigh(r, c, False))

    endpoints = [(int(r), int(c)) for r, c in coords if degree(int(r), int(c)) == 1]
    starts = endpoints + [(int(r), int(c)) for r, c in coords]
    polylines = []
    for sr, sc in starts:
        if visited[sr, sc]:
            continue
        line = []
        r, c = sr, sc
        prev = None
        while True:
            visited[r, c] = True
            line.append(pixel_to_lonlat(r, c, lat0, lon0, n))
            opts = list(neigh(r, c, True))
            if not opts:
                break
            if prev is not None and len(opts) > 1:
                pr, pc = prev
                vr, vc = r - pr, c - pc
                opts.sort(key=lambda rc: -((rc[0] - r) * vr + (rc[1] - c) * vc))
            prev = (r, c)
            r, c = opts[0]
        if len(line) >= 2:
            polylines.append(line)
    return polylines


def process_hgt(path, smooth_sigma, min_drop, min_axes, relief_std, downsample, min_deg, simplify_deg):
    lat0, lon0 = parse_hgt_name(path)
    dem = read_hgt(path)
    if downsample > 1:
        dem = dem[::downsample, ::downsample]
    mask = ridge_mask_dem(dem, smooth_sigma, min_drop, min_axes, relief_std)
    if mask.sum() < 10:
        return []
    skel = thin_connected(mask)
    raw = trace_skeleton(skel, lat0, lon0)
    out = []
    for pts in raw:
        ls = LineString(pts)
        if ls.length < min_deg:
            continue
        ls = ls.simplify(simplify_deg, preserve_topology=False)
        if not ls.is_empty and ls.length >= min_deg and ls.geom_type == "LineString":
            out.append((ls, {"contour": "ridge", "natural": "ridge", "source": "dem"}))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", type=Path, required=True)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--osm-pbf", type=Path, help="OSM extract (OsmAnd-style natural=ridge/arete)")
    src.add_argument("--hgt-dir", type=Path, help="Experimental: derive ridges from HGT DEM")
    ap.add_argument("--smooth-sigma", type=float, default=1.5)
    ap.add_argument("--min-drop", type=float, default=3.0)
    ap.add_argument("--min-axes", type=int, default=1, choices=(1, 2, 3, 4))
    ap.add_argument("--relief-std", type=float, default=15.0)
    ap.add_argument("--downsample", type=int, default=2)
    ap.add_argument("--min-length-deg", type=float, default=0.001)
    ap.add_argument("--simplify-deg", type=float, default=0.00025)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    items: list[tuple[LineString, dict[str, str]]] = []

    if args.osm_pbf:
        if not args.osm_pbf.exists():
            print(f"Missing {args.osm_pbf}", file=sys.stderr)
            return 1
        items = extract_osm_ridges(args.osm_pbf)
    else:
        hgts = sorted(args.hgt_dir.glob("*.hgt"))
        if args.limit:
            hgts = hgts[: args.limit]
        for i, hgt in enumerate(hgts, 1):
            print(f"[{i}/{len(hgts)}] {hgt.name} ...", flush=True)
            lines = process_hgt(
                hgt, args.smooth_sigma, args.min_drop, args.min_axes,
                args.relief_std, args.downsample, args.min_length_deg, args.simplify_deg,
            )
            avg = sum(len(ls.coords) for ls, _ in lines) / len(lines) if lines else 0
            print(f"  → {len(lines)} segments, avg {avg:.1f} nodes")
            items.extend(lines)

    write_osm(args.output, items)
    total = sum(ls.length for ls, _ in items)
    print(f"Wrote {len(items)} ways, total length {total:.2f}° → {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
