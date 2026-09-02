#!/usr/bin/env python3
"""Build the MapLibre sprite from the Garmin opentopomap-hike TYP file.

The Garmin map and the vector style must not drift apart, so both read
their symbols from garmin/style/typ/opentopomap-hike.txt. This script extracts the
XPM bitmaps of the point symbols (icons) and area fills (fill-patterns), renders
them into a sprite sheet and writes the accompanying sprite JSON.

Symbols that have an SVG of the same name in vector/symbols/ are taken from there
instead: the Garmin bitmaps are 8-16 px of pixel art and blur when a screen asks
for more, so the most prominent ones are authored as vector and rasterised here at
each sprite resolution. Requires rsvg-convert (librsvg) for those.

    python3 vector/tools/typ_to_sprite.py

No third-party Python dependencies: PNG is encoded and decoded with zlib/struct so
the script runs on a bare Python install.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_TYP = REPO / "garmin/style/typ/opentopomap-hike.txt"
DEFAULT_OUT = REPO / "vector/maplibregljs"
DEFAULT_SVG = REPO / "vector/symbols"
DEFAULT_NAME = "otm_sprite"

# Icon names match the "type" attribute written by process-otm.lua, so the
# style can use "icon-image": ["get", "type"].
ICONS = {
    "0x6616": ["peak", "volcano"],
    "0x661a": ["saddle"],
    "0x6619": ["cave", "sinkhole"],
    "0x661b": ["bridge"],
    "0x661c": ["ford"],
    "0x661d": ["camp_site"],
    # 0x2b07 ("hut") carries the same bitmap as the alpine hut, so both names share
    # this entry and its drawing instead of duplicating it.
    "0x661e": ["alpine_hut", "wilderness_hut"],
    "0x661f": ["shelter"],
    "0x6511": ["spring"],
    "0x6514": ["water_well"],
    "0x6414": ["drinking_water"],
    "0x6515": ["survey_point"],
    "0x6508": ["waterfall"],
    "0x6607": ["cliff_point"],
    "0x650a": ["glacier_point"],
    "0x2c0b": ["church"],
    "0x2c0d": ["castle", "castle_ruins"],
    "0x2c02": ["memorial", "cross", "wayside_cross"],
    "0x6411": ["tower"],
    "0x6701": ["observation_tower", "viewpoint"],
    "0x6700": ["communications_tower"],
    "0x6702": ["communications_mast"],
    "0x6412": ["water_tower"],
    "0x6415": ["watermill"],
    "0x6413": ["wind_turbine"],
    "0x6416": ["tree_broadleaved"],
    "0x6417": ["tree_needleleaved"],
    "0x2f0b": ["parking"],
    "0x2f17": ["bus_stop", "station"],
    "0x2e05": ["pharmacy"],
    "0x3002": ["hospital"],
    "0x2e02": ["supermarket"],
    "0x2e06": ["convenience"],
    "0x4c00": ["information"],
    "0x2d09": ["swimming"],
    "0x660f": ["barrier"],
}

# Area fills. Names are prefixed so they cannot collide with icon names.
PATTERNS = {
    "0x38": ["pattern-forest-conifer"],
    "0x39": ["pattern-forest-deciduous"],
    "0x50": ["pattern-forest"],
    "0x17": ["pattern-meadow"],
    "0x1c": ["pattern-farmland"],
    "0x4e": ["pattern-vineyard"],
    "0x11004": ["pattern-orchard"],
    "0x4f": ["pattern-scrub"],
    "0x58": ["pattern-fell"],
    "0x1a": ["pattern-cemetery"],
    "0x55": ["pattern-sand"],
    "0x56": ["pattern-scree"],
    "0x11005": ["pattern-quarry"],
    "0x57": ["pattern-bare-rock"],
    "0x54": ["pattern-moraine"],
    "0x51": ["pattern-wetland"],
    "0x04": ["pattern-exclusion"],
}

TRANSPARENT = (0, 0, 0, 0)


class Bitmap:
    def __init__(self, width: int, height: int, rows: list[list[tuple]]):
        self.width = width
        self.height = height
        self.rows = rows

    def scaled(self, factor: int) -> "Bitmap":
        if factor == 1:
            return self
        rows = []
        for row in self.rows:
            wide = [px for px in row for _ in range(factor)]
            rows.extend([list(wide) for _ in range(factor)])
        return Bitmap(self.width * factor, self.height * factor, rows)


def parse_color(token: str):
    token = token.strip()
    if token.lower() in ("none", "transparent"):
        return TRANSPARENT
    if token.startswith("#"):
        digits = token[1:]
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        if len(digits) != 6:
            raise ValueError(f"unsupported colour {token!r}")
        r, g, b = (int(digits[i : i + 2], 16) for i in (0, 2, 4))
        return (r, g, b, 255)
    named = {"black": (0, 0, 0, 255), "white": (255, 255, 255, 255)}
    if token.lower() in named:
        return named[token.lower()]
    raise ValueError(f"unsupported colour {token!r}")


def parse_blocks(text: str):
    """Yield (kind, type, bitmap-or-None) for every [_point]/[_line]/[_polygon] block."""
    for match in re.finditer(r"\[_(point|line|polygon)\](.*?)\[end\]", text, re.S):
        kind, body = match.group(1), match.group(2)
        type_match = re.search(r"^Type=(\S+)", body, re.M)
        if not type_match:
            continue
        yield kind, type_match.group(1).lower(), parse_xpm(body)


def parse_xpm(body: str):
    header = re.search(r'^(?:Day)?Xpm="(\d+)\s+(\d+)\s+(\d+)\s+(\d+)"', body, re.M)
    if not header:
        return None
    width, height, ncolors, cpp = (int(g) for g in header.groups())
    if width == 0 or height == 0 or cpp == 0:
        return None  # solid fill, no bitmap
    strings = re.findall(r'"([^"]*)"', body[header.end() :])
    if len(strings) < ncolors + height:
        raise ValueError(f"truncated XPM for block:\n{body[:120]}")
    palette = {}
    for entry in strings[:ncolors]:
        key, rest = entry[:cpp], entry[cpp:]
        colour = re.match(r"\s*c\s+(\S+)", rest)
        if not colour:
            raise ValueError(f"unparsable XPM colour line {entry!r}")
        palette[key] = parse_color(colour.group(1))
    rows = []
    for line in strings[ncolors : ncolors + height]:
        line = line.ljust(width * cpp)
        row = [palette.get(line[x * cpp : (x + 1) * cpp], TRANSPARENT) for x in range(width)]
        rows.append(row)
    return Bitmap(width, height, rows)


def encode_png(bitmap: Bitmap) -> bytes:
    raw = bytearray()
    for row in bitmap.rows:
        raw.append(0)  # filter type "none"
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", bitmap.width, bitmap.height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def decode_png(data: bytes) -> Bitmap:
    """Minimal reader for the 8-bit RGB/RGBA PNGs that rsvg-convert writes."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos = 8
    header = None
    idat = bytearray()
    while pos < len(data):
        length, tag = struct.unpack(">I4s", data[pos : pos + 8])
        payload = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, depth, colour, compression, filt, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if depth != 8 or colour not in (2, 6) or compression or filt or interlace:
                raise ValueError(f"unsupported PNG: depth={depth} colour={colour}")
            header = (width, height, 4 if colour == 6 else 3)
        elif tag == b"IDAT":
            idat += payload
        elif tag == b"IEND":
            break
    if header is None:
        raise ValueError("PNG without IHDR")
    width, height, channels = header
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    rows: list[list[tuple]] = []
    previous = bytearray(stride)
    at = 0
    for _ in range(height):
        filter_type = raw[at]
        line = bytearray(raw[at + 1 : at + 1 + stride])
        at += 1 + stride
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up = previous[i]
            up_left = previous[i - channels] if i >= channels else 0
            if filter_type == 1:
                line[i] = (line[i] + left) & 0xFF
            elif filter_type == 2:
                line[i] = (line[i] + up) & 0xFF
            elif filter_type == 3:
                line[i] = (line[i] + (left + up) // 2) & 0xFF
            elif filter_type == 4:
                estimate = left + up - up_left
                candidates = (
                    (abs(estimate - left), left),
                    (abs(estimate - up), up),
                    (abs(estimate - up_left), up_left),
                )
                line[i] = (line[i] + min(candidates)[1]) & 0xFF
            elif filter_type != 0:
                raise ValueError(f"unsupported PNG filter {filter_type}")
        rows.append(
            [
                tuple(line[x * channels : x * channels + channels]) + ((255,) if channels == 3 else ())
                for x in range(width)
            ]
        )
        previous = line
    return Bitmap(width, height, rows)


def render_svg(path: Path, factor: int) -> Bitmap:
    """Rasterise at factor times the SVG's own size, which is the 1x sprite size."""
    if shutil.which("rsvg-convert") is None:
        raise SystemExit("rsvg-convert not found on PATH, needed for vector/symbols/*.svg")
    result = subprocess.run(
        ["rsvg-convert", "--zoom", str(factor), "--format", "png", str(path)],
        check=True,
        stdout=subprocess.PIPE,
    )
    return decode_png(result.stdout)


def svg_overrides(svg_dir: Path) -> dict[str, Path]:
    """Map symbol name -> SVG. A TYP type whose aliases share one bitmap keeps
    sharing one drawing, so peak.svg also covers the volcano alias."""
    if not svg_dir.is_dir():
        return {}
    available = {path.stem: path for path in sorted(svg_dir.glob("*.svg"))}
    overrides: dict[str, Path] = {}
    for table in (ICONS, PATTERNS):
        for aliases in table.values():
            own = {alias: available[alias] for alias in aliases if alias in available}
            if not own:
                continue
            fallback = next(iter(own.values()))
            for alias in aliases:
                overrides[alias] = own.get(alias, fallback)
    unused = sorted(set(available) - set(overrides))
    if unused:
        print(f"warning: SVGs match no known symbol: {', '.join(unused)}", file=sys.stderr)
    return overrides


def pack(entries: list[tuple[str, Bitmap]], padding: int, max_width: int):
    """Shelf-pack tallest-first and return (sheet, placements)."""
    ordered = sorted(entries, key=lambda item: (-item[1].height, item[0]))
    placements: dict[str, tuple[int, int, Bitmap]] = {}
    x = y = shelf_height = 0
    total_width = 0
    for name, bitmap in ordered:
        if x > 0 and x + bitmap.width > max_width:
            x = 0
            y += shelf_height + padding
            shelf_height = 0
        placements[name] = (x, y, bitmap)
        x += bitmap.width + padding
        total_width = max(total_width, x - padding)
        shelf_height = max(shelf_height, bitmap.height)
    total_height = y + shelf_height
    sheet = Bitmap(
        total_width,
        total_height,
        [[TRANSPARENT] * total_width for _ in range(total_height)],
    )
    for name, (px, py, bitmap) in placements.items():
        for dy, row in enumerate(bitmap.rows):
            target = sheet.rows[py + dy]
            for dx, pixel in enumerate(row):
                target[px + dx] = pixel
    return sheet, placements


def build(typ_path: Path, out_dir: Path, svg_dir: Path, name: str, icon_scale: int, pattern_scale: int):
    text = typ_path.read_text(encoding="utf-8", errors="replace")
    bitmaps: dict[str, Bitmap] = {}
    for kind, typ, bitmap in parse_blocks(text):
        table = ICONS if kind == "point" else PATTERNS
        names = table.get(typ)
        if not names or bitmap is None:
            continue
        scale = icon_scale if kind == "point" else pattern_scale
        for alias in names:
            bitmaps[alias] = bitmap.scaled(scale)

    overrides = svg_overrides(svg_dir)
    missing = [
        typ
        for table in (ICONS, PATTERNS)
        for typ in table
        if not all(alias in bitmaps or alias in overrides for alias in table[typ])
    ]
    if missing:
        print(f"warning: no bitmap found for TYP types {', '.join(sorted(missing))}", file=sys.stderr)
    if not bitmaps and not overrides:
        raise SystemExit(f"no symbols extracted from {typ_path}")

    out_dir.mkdir(parents=True, exist_ok=True)
    for suffix, factor, ratio in (("", 1, 1), ("@2x", 2, 2)):
        entries = [
            (alias, bmp.scaled(factor))
            for alias, bmp in bitmaps.items()
            if alias not in overrides
        ]
        entries += [(alias, render_svg(path, factor)) for alias, path in overrides.items()]
        sheet, placements = pack(entries, padding=2 * factor, max_width=512 * factor)
        index = {
            alias: {
                "x": x,
                "y": y,
                "width": bmp.width,
                "height": bmp.height,
                "pixelRatio": ratio,
            }
            for alias, (x, y, bmp) in placements.items()
        }
        png_path = out_dir / f"{name}{suffix}.png"
        json_path = out_dir / f"{name}{suffix}.json"
        png_path.write_bytes(encode_png(sheet))
        json_path.write_text(json.dumps(index, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"{png_path.relative_to(REPO)}: {sheet.width}x{sheet.height}, {len(index)} symbols")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--typ", type=Path, default=DEFAULT_TYP, help="Garmin TYP source file")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory")
    parser.add_argument("--svg", type=Path, default=DEFAULT_SVG, help="directory with SVG overrides")
    parser.add_argument("--name", default=DEFAULT_NAME, help="sprite base name")
    parser.add_argument(
        "--icon-scale",
        type=int,
        default=2,
        help="upscale factor for point symbols (Garmin icons are 5-20 px, too small for a web map)",
    )
    parser.add_argument("--pattern-scale", type=int, default=1, help="upscale factor for area fills")
    args = parser.parse_args(argv)
    build(args.typ, args.out, args.svg, args.name, args.icon_scale, args.pattern_scale)


if __name__ == "__main__":
    main()
