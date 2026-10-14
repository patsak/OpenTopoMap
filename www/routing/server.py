"""Hiking routes through the mountains, on and off trail.

    OTM_ROUTING_CONFIG=routing/config.yaml python -m routing.server

    POST /route/api/v1/route    GeoJSON points in, GeoJSON route out
    GET  /route/api/v1/areas    the configured areas and their graphs
    GET  /route/health
    GET  /route/                a map to click a route on (debugging)

The request is GeoJSON: a FeatureCollection of Points (visited in order), or a
MultiPoint / LineString geometry or feature. Options go in its "properties"
(or the query string): ``allow_offroad`` (default true), ``pack_kg``, the
backpack in kilograms (default 0; routing.pack), and the pass limits
(routing.passlimit): ``max_pass_grade`` (rtsa_scale, e.g. "1Б"),
``max_top_passes`` (how many of that grade at most) and ``allow_unrated``
(default true).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# www/ (or /app in Docker), so `python server.py` finds the package too.
sys.path.insert(0, str(ROOT.parent))

from flask import Flask, Response, jsonify, request, send_from_directory  # noqa: E402

from routing import config as routing_config  # noqa: E402
from routing import model  # noqa: E402
from routing.config import PackParams  # noqa: E402
from routing.engine.base import Options, RouteError  # noqa: E402
from routing.graphstore import CompiledStore  # noqa: E402
from routing.package import PackageError  # noqa: E402
from routing.route import to_geojson  # noqa: E402

log = logging.getLogger("routing")

MAX_POINTS = 50
TRUE = frozenset({"1", "true", "yes", "on"})


def create_app(cfg: routing_config.Config | None = None, store=None) -> Flask:
    app = Flask(__name__, static_folder=None)
    cfg = cfg or routing_config.load()
    store = store or CompiledStore(cfg)
    app.config["ROUTING_STORE"] = store

    @app.after_request
    def allow_any_origin(response: Response) -> Response:
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    @app.route("/route/api/v1/route", methods=["POST", "OPTIONS"])
    def route():
        if request.method == "OPTIONS":
            return Response(status=204, headers={"Access-Control-Allow-Headers": "Content-Type"})
        body = request.get_json(silent=True)
        try:
            points, options = parse_request(body, request.args, cfg.pack)
            area = store.area_for(points)
            loaded = store.get(area.name)
            path = loaded.engine.route(points, options)
        except RouteError as exc:
            return _error(str(exc), exc.status)
        except PackageError as exc:
            log.warning("%s", exc)
            return _error(str(exc), 503)
        except Exception:  # noqa: BLE001 - a bug must not take the worker down silently
            log.exception("route failed: %s", body)
            return _error("the route failed inside the service; see its log", 500)
        body = to_geojson(loaded.graph, path, area.name, options.pack_kg,
                          edge_time=loaded.engine.edge_time(path, options))
        return jsonify(body)

    @app.get("/route/api/v1/areas")
    def areas():
        features = []
        for entry in store.status():
            w, s, e, n = entry["bbox"]
            geometry = entry.pop("geometry", None) or {
                "type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}
            features.append({"type": "Feature", "geometry": geometry, "properties": entry})
        return jsonify({"type": "FeatureCollection", "features": features})

    @app.get("/route/health")
    def health():
        status = store.status()
        built = [a["name"] for a in status if a["built"]]
        body = {"status": "ok" if built else "unavailable", "areas": status}
        return jsonify(body), (200 if built else 503)

    @app.get("/route/")
    def index():
        return send_from_directory(ROOT / "static", "index.html")

    return app


def _flag(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in TRUE


def parse_request(body, args, pack_params: PackParams | None = None) -> tuple[list[tuple[float, float]], Options]:
    pack_params = pack_params or PackParams()
    if not isinstance(body, dict):
        raise RouteError("the body must be GeoJSON", 400)
    kind = body.get("type")
    points: list = []
    if kind == "FeatureCollection":
        for feature in body.get("features") or []:
            geometry = (feature or {}).get("geometry") or {}
            if geometry.get("type") != "Point":
                raise RouteError("a FeatureCollection request holds Point features only", 400)
            points.append(geometry.get("coordinates"))
    else:
        geometry = body.get("geometry") if kind == "Feature" else body
        if not isinstance(geometry, dict) or geometry.get("type") not in ("MultiPoint", "LineString"):
            raise RouteError("send a FeatureCollection of Points, a MultiPoint or a LineString", 400)
        points = geometry.get("coordinates") or []
    if not 2 <= len(points) <= MAX_POINTS:
        raise RouteError(f"a route needs 2 to {MAX_POINTS} points, not {len(points)}", 400)
    out = []
    for p in points:
        try:
            lon, lat = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            raise RouteError(f"not a position: {p!r}", 400) from None
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise RouteError(f"not a lon/lat position: {p!r}", 400)
        out.append((lon, lat))

    props = body.get("properties") or body.get("options") or {}
    allow = props.get("allow_offroad", True) if isinstance(props, dict) else True
    if "allow_offroad" in args:
        allow = args["allow_offroad"]
    kg = props.get("pack_kg", 0) if isinstance(props, dict) else 0
    kg = args.get("pack_kg", kg)
    try:
        kg = float(kg or 0)
    except (TypeError, ValueError):
        raise RouteError(f"pack_kg must be a number of kilograms, not {kg!r}", 400) from None
    if not 0 <= kg <= pack_params.max_kg:
        raise RouteError(f"pack_kg must be within 0..{pack_params.max_kg:g}", 400)

    def option(name, default=None):
        value = props.get(name, default) if isinstance(props, dict) else default
        return args.get(name, value)

    max_grade = option("max_pass_grade")
    grade = None
    if max_grade not in (None, ""):
        grade = model.pass_grade(str(max_grade))
        if grade == model.UNRATED:
            raise RouteError(f"max_pass_grade must be one of {', '.join(model.GRADES)}, not {max_grade!r}", 400)
    top = option("max_top_passes")
    if top not in (None, ""):
        try:
            top = int(top)
        except (TypeError, ValueError):
            raise RouteError(f"max_top_passes must be a whole number, not {top!r}", 400) from None
        if top < 0:
            raise RouteError("max_top_passes cannot be negative", 400)
    else:
        top = None
    return out, Options(allow_offroad=_flag(allow), pack_kg=kg, pack=pack_params, max_pass_grade=grade,
                        max_top_passes=top, allow_unrated=_flag(option("allow_unrated", True)))


def _error(message: str, status: int) -> tuple[Response, int]:
    return jsonify({"error": message}), status


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = int(os.environ.get("OTM_PORT", "8080"))
    create_app().run(host=os.environ.get("OTM_HOST", "127.0.0.1"), port=port, threaded=True)
