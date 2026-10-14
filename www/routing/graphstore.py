"""The compiled graphs, as the service sees them.

A graph is mapped on first use, not at startup, and mapped again whenever
routing.compile has swapped in a new one (its meta.json changed identity). So
the service can start before any graph exists - it answers 503 until then -
and picks up the nightly rebuild without a restart.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from routing.config import Config
from routing.engine.base import Engine, RouteError
from routing.package import PackageError

log = logging.getLogger(__name__)


@dataclass
class Loaded:
    graph: object  # what the engine routes on, and routing.route reads the route off
    engine: Engine
    identity: tuple[int, int, int]


def _disk(path: Path) -> Engine:
    from routing.engine.disk import DiskEngine, DiskGraph

    return DiskEngine(DiskGraph(path))


# OTM_ROUTING_ENGINE -> an engine over the compiled graph in a directory
ENGINES: dict[str, Callable[[Path], Engine]] = {"disk": _disk}


def engine_name() -> str:
    """OTM_ROUTING_ENGINE, default disk."""
    return os.environ.get("OTM_ROUTING_ENGINE", "disk").strip() or "disk"


def engine_factory() -> Callable[[Path], Engine]:
    name = engine_name()
    if name not in ENGINES:
        raise ValueError(f"OTM_ROUTING_ENGINE={name!r} is not an engine this service has "
                         f"({', '.join(ENGINES)})")
    return ENGINES[name]


@dataclass(frozen=True)
class GraphArea:
    """A compiled graph as the service picks it: by where it covers."""

    name: str
    bbox: tuple[float, float, float, float]
    shape: object  # shapely geometry, prepared


class CompiledStore:
    """Compiled graphs (routing.compile) under <routing>/graphs/, each routed
    on by the configured engine. The directory is rescanned when it
    changes, so a newly compiled region is offered without a restart."""

    def __init__(self, cfg: Config, factory: Callable[[Path], Engine] | None = None):
        self.cfg = cfg
        self.factory = factory or engine_factory()
        self.root = cfg.routing_dir / "graphs"
        self._lock = threading.Lock()
        self._loaded: dict[str, Loaded] = {}
        self._areas: list[GraphArea] = []
        self._scanned: tuple | None = None

    def _scan(self) -> list[GraphArea]:
        import shapely

        try:
            metas = sorted(self.root.glob("*/meta.json"))
            stamp = tuple((m, m.stat().st_mtime_ns) for m in metas)
        except FileNotFoundError:
            metas, stamp = [], ()
        with self._lock:
            if stamp != self._scanned:
                areas = []
                for m in metas:
                    meta = json.loads(m.read_text(encoding="utf-8"))
                    shape = shapely.geometry.shape(meta["coverage"])
                    shapely.prepare(shape)
                    areas.append(GraphArea(meta["name"], tuple(meta["bbox"]), shape))
                self._areas, self._scanned = areas, stamp
            return self._areas

    def get(self, name: str) -> Loaded:
        meta = self.root / name / "meta.json"
        try:
            st = meta.stat()
        except FileNotFoundError:
            raise PackageError(f"graph {name} is not compiled yet; run python -m routing.compile") from None
        identity = (st.st_ino, st.st_size, st.st_mtime_ns)
        with self._lock:
            loaded = self._loaded.get(name)
            if loaded is None or loaded.identity != identity:
                engine = self.factory(self.root / name)
                baked = getattr(engine.graph, "meta", {}).get("pack")
                if baked is not None and baked != dataclasses.asdict(self.cfg.pack):
                    log.warning("graph %s was compiled with other pack coefficients than the config's; "
                                "the backpack follows the graph's until python -m routing.compile", name)
                loaded = Loaded(engine.graph, engine, identity)
                self._loaded[name] = loaded
                log.info("graph %s: %s engine, compiled %s", name, engine.name,
                         json.loads(meta.read_text(encoding="utf-8")).get("compiled_at"))
            return loaded

    def area_for(self, points: list[tuple[float, float]]) -> GraphArea:
        """The graph covering every point; the smallest if several do."""
        import shapely

        pts = shapely.points(np.asarray(points))
        areas = self._scan()
        if not areas:
            raise PackageError("no routing graph is compiled yet; run python -m routing.compile")
        hits = [a for a in areas if shapely.contains(a.shape, pts).all()]
        if not hits:
            names = ", ".join(a.name for a in self._scan()) or "none"
            raise RouteError(f"the points are not all inside one routing area (areas: {names})")
        return min(hits, key=lambda a: a.shape.area)

    def status(self) -> list[dict]:
        out = []
        for area in self._scan():
            meta = json.loads((self.root / area.name / "meta.json").read_text(encoding="utf-8"))
            out.append({"name": area.name, "bbox": list(area.bbox), "built": True,
                        "geometry": meta["coverage"], "built_at": meta.get("built_at"),
                        "compiled_at": meta.get("compiled_at"), "nodes": meta.get("nodes"),
                        "edges": meta.get("edges_by_kind"), "region": meta.get("region")})
        return out
