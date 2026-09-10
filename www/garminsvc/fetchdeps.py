"""Install the pinned Java tooling into a directory.

Both the Docker build and a local checkout come through here, so the versions
and their checksums are stated once, in `artifacts.py`:

    python -m garminsvc.fetchdeps                    # into constants.TOOLS_DIR
    python -m garminsvc.fetchdeps --dest /opt/garmin-tools
    python -m garminsvc.fetchdeps mkgmap             # just one of them

For the sea/bounds data as well, use `download_deps.py` at the service root.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from garminsvc import artifacts
from garminsvc.constants import TOOLS_DIR


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "names",
        nargs="*",
        metavar="NAME",
        help=f"artifacts to install (default: all of {', '.join(sorted(artifacts.ARTIFACTS))})",
    )
    parser.add_argument("--dest", type=Path, default=TOOLS_DIR, help=f"install directory (default: {TOOLS_DIR})")
    args = parser.parse_args(argv)

    try:
        jars = artifacts.install_all(args.dest, log=print, names=args.names or None)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for name, jar in jars.items():
        print(f"  {name:9s}= {jar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
