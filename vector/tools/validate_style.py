#!/usr/bin/env python3
"""Check the MapLibre style against its data, sprite and the Garmin TYP palette.

Catches the drift that is otherwise only visible as a silently empty map:

  * a source-layer that the tilemaker config does not produce
  * an icon-image or fill-pattern that is missing from the sprite
  * a colour that does not appear in the Garmin TYP palette
  * duplicate layer ids or a style file that is not parseable

    python3 vector/tools/validate_style.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STYLE = REPO / "vector/maplibregljs/otm_layers.json"
SPRITE = REPO / "vector/maplibregljs/otm_sprite.json"
# The OSM tileset's layers. The ocean tileset is built from the same lua with
# its own config, so both are read to get every source-layer the style may use.
CONFIGS = (
    REPO / "vector/tilemaker/tilemaker-config-otm-region.json",
    REPO / "vector/tilemaker/tilemaker-config-otm-ocean.json",
)
TYP_FILES = [
    REPO / "garmin/style/typ/opentopomap-hike.txt",
    REPO / "garmin/style/typ/contours-hike.txt",
]

# Sources that are not vector tiles from the hike tileset.
NON_TILE_SOURCES = {"dem", "contour-source"}
# Colours that are deliberately not in the TYP palette.
ALLOWED_EXTRA_COLORS = {
    "#ffffff",  # halo and glacier ice, also 0x4d
    "#000000",  # casing, footpaths, cliffs
}


def load_style(path: Path):
    body = path.read_text(encoding="utf-8")
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"(?m)^\s*//.*$", "", body)
    body = re.sub(r"//[^\"'\n]*$", "", body, flags=re.M)
    body = re.sub(r"^\s*(?:const|var|let)\s+\w+\s*=", "", body)
    body = body.strip().rstrip(";").strip()
    body = re.sub(r",(\s*[}\]])", r"\1", body)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path.name}: cannot parse style ({exc})")


def tile_layers(*config_paths: Path) -> set[str]:
    """Source-layers the given tilemaker configs emit.

    ``write_to`` is what lands in the tile: a config entry named ``land_low``
    writing to ``land`` is a zoom range of the ``land`` layer, not a layer of
    its own, and the style only ever names the latter.
    """
    names: set[str] = set()
    for path in config_paths:
        config = json.loads(path.read_text(encoding="utf-8"))
        for name, spec in config["layers"].items():
            names.add(spec.get("write_to", name))
    return names


def walk_strings(node, key_filter, out: list[str], inside=False):
    """Collect string literals below any key matching key_filter."""
    if isinstance(node, dict):
        for key, value in node.items():
            walk_strings(value, key_filter, out, inside or key_filter(key))
    elif isinstance(node, list):
        for value in node:
            walk_strings(value, key_filter, out, inside)
    elif isinstance(node, str) and inside:
        out.append(node)


def collect_outputs(expr, out: list[str]):
    """Collect the output values of an expression, skipping match labels and inputs."""
    if isinstance(expr, str):
        out.append(expr)
        return
    if not isinstance(expr, list) or not expr or not isinstance(expr[0], str):
        return
    op, args = expr[0], expr[1:]
    if not args:
        return
    if op == "match":
        # [match, input, label, output, ..., default]
        for index in range(2, len(args) - 1, 2):
            collect_outputs(args[index], out)
        collect_outputs(args[-1], out)
    elif op in ("case", "step"):
        # [case, cond, out, ..., default] / [step, input, out, stop, out, ...]
        for index in range(1, len(args) - 1, 2):
            collect_outputs(args[index], out)
        collect_outputs(args[-1], out)
    elif op == "coalesce":
        for arg in args:
            collect_outputs(arg, out)
    elif op == "image":
        if args and isinstance(args[0], str):
            out.append(args[0])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--style", type=Path, default=STYLE)
    parser.add_argument("--sprite", type=Path, default=SPRITE)
    parser.add_argument("--config", type=Path, action="append", default=None)
    args = parser.parse_args(argv)

    configs = tuple(args.config) if args.config else CONFIGS
    layers = load_style(args.style)
    sprite = set(json.loads(args.sprite.read_text(encoding="utf-8")))
    available = tile_layers(*configs)
    palette = {
        colour.lower()
        for path in TYP_FILES
        if path.exists()
        for colour in re.findall(r"#[0-9a-fA-F]{6}", path.read_text(encoding="utf-8", errors="replace"))
    }

    errors: list[str] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()

    for layer in layers:
        lid = layer.get("id", "<no id>")
        if lid in seen_ids:
            errors.append(f"duplicate layer id {lid!r}")
        seen_ids.add(lid)

        source = layer.get("source")
        source_layer = layer.get("source-layer")
        if source and source not in NON_TILE_SOURCES and not source_layer:
            errors.append(f"{lid}: vector layer without source-layer")
        if source_layer and source not in NON_TILE_SOURCES and source_layer not in available:
            errors.append(
                f"{lid}: source-layer {source_layer!r} is not produced by any of "
                + ", ".join(path.name for path in configs)
            )

        images: list[str] = []
        for prop in ("icon-image",):
            if prop in layer.get("layout", {}):
                collect_outputs(layer["layout"][prop], images)
        for prop in ("fill-pattern", "line-pattern"):
            if prop in layer.get("paint", {}):
                collect_outputs(layer["paint"][prop], images)
        for image in images:
            if image.startswith("{"):
                continue
            if image not in sprite:
                errors.append(f"{lid}: sprite has no image {image!r}")

        colours: list[str] = []
        walk_strings(layer.get("paint", {}), lambda k: k.endswith("color"), colours)
        for colour in colours:
            match = re.fullmatch(r"#[0-9a-fA-F]{6}", colour)
            if not match:
                continue  # rgba()/named colours are intentional deviations
            value = colour.lower()
            if value not in palette and value not in ALLOWED_EXTRA_COLORS:
                warnings.append(f"{lid}: colour {colour} is not in the Garmin TYP palette")

    print(f"{len(layers)} layers, {len(sprite)} sprite images, {len(palette)} palette colours")
    for warning in warnings:
        print(f"warning: {warning}")
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if errors:
        return 1
    print("style ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
