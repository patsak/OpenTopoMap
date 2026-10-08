"""An Overpass API endpoint answering from a GeoDesk GOL.

    OTM_OVERPASS_GOL=/path/to/regions.gol python -m overpass.server

The routes are the ones clients call on overpass-api.de:

    GET/POST /api/interpreter   the query, as ``data=`` or as the raw body
    GET      /api/status        the slot report
    GET      /api/kill_my_queries
    GET      /health

Run it under gunicorn with sync workers and one thread each (see the
Dockerfile): every worker opens the GOL for itself, and a query holds its
worker until it is done.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent
# www/ (or /app in Docker), so `python server.py` finds the package too.
sys.path.insert(0, str(ROOT.parent))

from flask import Flask, Response, request

from overpass import evaluator, output
from overpass.library import Library, LibraryUnavailable
from overpass.parser import QLError, parse

log = logging.getLogger("overpass")

# What datasvc builds (otmlib.paths.regions_gol), in the stack's layout.
DEFAULT_GOL = "/app/data/gol/regions.gol"
# The ceiling a script's own [timeout:] cannot raise. Overpass's default is 180.
MAX_TIMEOUT = 600
DEFAULT_MAX_ELEMENTS = 2_000_000


def create_app(gol_path: str | None = None) -> Flask:
    app = Flask(__name__)
    library = Library(gol_path or os.environ.get("OTM_OVERPASS_GOL", DEFAULT_GOL))
    max_timeout = int(os.environ.get("OTM_OVERPASS_MAX_TIMEOUT", MAX_TIMEOUT))
    max_elements = int(os.environ.get("OTM_OVERPASS_MAX_ELEMENTS", DEFAULT_MAX_ELEMENTS))
    app.config["OVERPASS_LIBRARY"] = library

    @app.after_request
    def allow_any_origin(response: Response) -> Response:
        # overpass-turbo and other web clients call Overpass cross-origin.
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    @app.route("/api/interpreter", methods=["GET", "POST", "OPTIONS"])
    def interpreter():
        if request.method == "OPTIONS":
            return Response(status=204, headers={"Access-Control-Allow-Headers": "Content-Type"})
        text = _query_text()
        if not text.strip():
            return _error(["line 1: parse error: empty query"])
        if text.lstrip().startswith("<"):
            return _error(["line 1: static error: the XML query language is not supported, only Overpass QL"])
        try:
            script = parse(text)
        except QLError as exc:
            return _error([str(exc)])
        script.settings.timeout = min(script.settings.timeout, max_timeout)

        try:
            world = library.features()
        except LibraryUnavailable as exc:
            log.warning("%s", exc)
            return _error([f"runtime error: {exc}"], status=503)

        try:
            result = evaluator.run(world, script.settings, script.statements, max_elements=max_elements)
        except Exception:  # noqa: BLE001 - a bug here must not take the worker down silently
            log.exception("query failed: %s", text)
            return _error(["runtime error: the query failed inside the service; see its log"], status=500)
        if result.remark:
            log.info("query ended with a remark: %s", result.remark)
        body, mimetype = output.render(script.settings, result, timestamp=library.timestamp())
        return Response(body, mimetype=mimetype)

    @app.get("/api/status")
    def status():
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        text = (
            f"Connected as: {request.remote_addr}\n"
            f"Current time: {now}\n"
            "Announced endpoint: none\n"
            "Rate limit: 0\n"
            "1 slots available now.\n"
            "Currently running queries (pid, space limit, time limit, start time):\n"
        )
        return Response(text, mimetype="text/plain")

    @app.get("/api/kill_my_queries")
    def kill_my_queries():
        # Nothing to kill: a query lives exactly as long as its own request.
        return Response("", mimetype="text/plain")

    @app.get("/health")
    def health():
        try:
            library.features()
        except LibraryUnavailable as exc:
            return {"status": "unavailable", "error": str(exc)}, 503
        return {"status": "ok", "osm_base": library.timestamp()}

    return app


def _query_text() -> str:
    """The script, wherever the client put it.

    Overpass takes ``data=`` from the query string or a form body, and a body
    that is not a form is the script itself - which is how curl --data-binary
    and most scripts send it.
    """
    if "data" in request.args:
        return request.args["data"]
    if request.method != "POST":
        return ""
    # The body first, and cached: `curl -d 'node(1);out;'` labels a bare script
    # as a form, and once Flask has parsed it as one the text is gone.
    raw = request.get_data(cache=True, as_text=True)
    if "data" in request.form:
        return request.form["data"]
    if raw.startswith("data="):
        return parse_qs(raw, keep_blank_values=True).get("data", [""])[0]
    return raw


def _error(messages: list[str], status: int = 400) -> Response:
    return Response(output.error_page(messages), status=status, mimetype="text/html")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    port = int(os.environ.get("OTM_PORT", "8080"))
    # threaded=False: one query at a time, as under gunicorn's sync workers.
    create_app().run(host=os.environ.get("OTM_HOST", "127.0.0.1"), port=port, threaded=False)
