"""Fill the shared data directory with the sea and bounds archives.

The counterpart of `fetchdeps`: that one installs the pinned jars into the
image, this one fetches the several gigabytes of precomputed coastline and
boundary data that mkgmap needs at build time. It is data rather than tooling —
unversioned `*-latest.zip`, far too large to bake in — so it lives in the volume
the services share and is filled once, before anything tries to build a map:

    python -m garminsvc.fetchdata

Both are no-ops when the data is already in place, which is what makes this
safe to run on every `docker compose up` (see the `garminsvc-init` service).
Downloading it here rather than from the server's app factory is the point: a
first boot otherwise spent a quarter of an hour inside gunicorn's worker before
answering a single request.
"""

from __future__ import annotations

import argparse
import sys

from garminsvc.constants import DATA_DIR
from garminsvc.deps import download_sea_bounds, sea_bounds_ready


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report whether the data is in place and exit; download nothing",
    )
    args = parser.parse_args(argv)

    if args.check:
        ready = sea_bounds_ready()
        print(f"sea/bounds in {DATA_DIR}: {'ready' if ready else 'missing'}")
        return 0 if ready else 1

    try:
        sea, bounds = download_sea_bounds(log=print)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("OK")
    print(f"  sea    = {sea}")
    print(f"  bounds = {bounds}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
