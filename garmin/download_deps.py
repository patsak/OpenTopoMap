#!/usr/bin/env python3
"""Download external Garmin map build dependencies (mkgmap, splitter, sea, bounds).

Does not start the HTTP server. Run once before `python server.py`:

  pip install -r requirements-server.txt
  python download_deps.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from mapsvc.deps import download_deps


def main() -> int:
    try:
        deps = download_deps(log=print)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("OK")
    print(f"  java     = {deps.java}")
    print(f"  osmium   = {deps.osmium}")
    print(f"  mkgmap   = {deps.mkgmap_jar}")
    print(f"  splitter = {deps.splitter_jar}")
    print(f"  sea      = {deps.sea_dir}")
    print(f"  bounds   = {deps.bounds_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
