#!/usr/bin/env python3
"""HTTP service for building OpenTopoMap Garmin hike maps by bbox.

Prerequisites (install separately, service will refuse to start without them):

  pip install -r requirements-server.txt
  python -m garminsvc.download_deps

Nothing is downloaded from here. Preparing the jars and the sea/bounds data is a
step of its own — `garminsvc.fetchdeps` and `garminsvc.fetchdata`, run by the
Dockerfile and the `garminsvc-init` compose service respectively — so a first
boot cannot spend a quarter of an hour inside gunicorn's app factory before the
process answers anything.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# One level up holds both packages: /app in Docker, www/ in a checkout. Needed
# when this file is run directly (`python server.py`) rather than imported as
# `garminsvc.server`.
sys.path.insert(0, str(ROOT.parent))

from flask import Flask, g, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import RequestEntityTooLarge

from garminsvc.client import CLIENT_COOKIE, CLIENT_COOKIE_MAX_AGE, resolve_client_id
from garminsvc.constants import GEOFABRIK_CACHE, JOBS_DIR, MAX_UPLOAD_BYTES, PREVIEWS_DIR
from garminsvc.deps import require_deps
from garminsvc.job import JobStatus, job_download_filename, normalize_job_name
from garminsvc.jobs import job_manager
from garminsvc.osmfile import UploadError, normalize_upload_name, save_upload_stream
from garminsvc.vectorbasemap import LAYERS_ASSET, preview_tiles_url, style_dir
from garminsvc.vectorbasemap import config as vector_config
from otmlib import previewqueue, previews, regionsync
from otmlib.bbox import parse_bbox

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("server")

STATIC_DIR = ROOT / "static"
app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 1024 * 1024
app.config["MAX_FORM_MEMORY_SIZE"] = 16 * 1024 * 1024


@app.before_request
def bind_client() -> None:
    g.client_id, g.set_client_cookie = resolve_client_id(request.cookies.get(CLIENT_COOKIE))


@app.after_request
def emit_client_cookie(response):
    if getattr(g, "set_client_cookie", False):
        response.set_cookie(
            CLIENT_COOKIE,
            g.client_id,
            max_age=CLIENT_COOKIE_MAX_AGE,
            httponly=True,
            samesite="Lax",
            secure=request.is_secure,
            path="/",
        )
    return response


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.post("/maps")
def create_map():
    payload = request.get_json(silent=True) or {}
    try:
        west, south, east, north = parse_bbox(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    job = job_manager.create(
        west,
        south,
        east,
        north,
        name=normalize_job_name(str(payload.get("name") or "")),
        owner_id=g.client_id,
    )
    stats = job_manager.queue_stats()
    return jsonify(
        {
            "job_id": job.job_id,
            "status": job.status.value,
            "name": job.name,
            "queued": stats["queued"],
            "running": stats["running"],
            "cancellable": job.can_cancel(g.client_id),
        }
    ), 202


@app.errorhandler(413)
@app.errorhandler(RequestEntityTooLarge)
def too_large(_exc):
    return jsonify({"error": f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB"}), 413


@app.post("/maps/upload")
def create_map_upload():
    name = normalize_job_name(str(request.form.get("name") or ""))
    if not name:
        return jsonify({"error": "Give the map a name"}), 400
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "An .osm or .osm.pbf file is required"}), 400
    try:
        filename = normalize_upload_name(uploaded.filename)
    except UploadError as exc:
        return jsonify({"error": str(exc)}), 400

    job_id = str(uuid.uuid4())
    dest = JOBS_DIR / job_id / filename
    try:
        size = save_upload_stream(uploaded.stream, dest, declared_size=None)
    except UploadError as exc:
        shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)
        return jsonify({"error": str(exc)}), 400

    job = job_manager.create(
        0,
        0,
        0,
        0,
        name=name,
        source_pbf=str(dest),
        job_id=job_id,
        owner_id=g.client_id,
    )
    stats = job_manager.queue_stats()
    return jsonify(
        {
            "job_id": job.job_id,
            "status": job.status.value,
            "name": job.name,
            "bytes": size,
            "queued": stats["queued"],
            "running": stats["running"],
            "cancellable": job.can_cancel(g.client_id),
        }
    ), 202


@app.get("/jobs")
def list_jobs():
    jobs = job_manager.list_recent()
    return jsonify(
        {"jobs": [job.to_summary(g.client_id) for job in jobs], **job_manager.queue_stats()}
    )


@app.get("/jobs/<job_id>")
def get_job(job_id: str):
    job = job_manager.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    data = job.to_dict(g.client_id)
    data.update(job_manager.queue_stats())
    return jsonify(data)


@app.post("/jobs/<job_id>/cancel")
def cancel_job(job_id: str):
    job = job_manager.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
        return jsonify({"error": f"Cannot cancel a {job.status.value} job"}), 409
    if not job.can_cancel(g.client_id):
        return jsonify({"error": "Only whoever started this build can cancel it"}), 403
    job = job_manager.cancel(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    data = job.to_dict(g.client_id)
    data.update(job_manager.queue_stats())
    return jsonify(data)


@app.get("/queue")
def get_queue():
    return jsonify(job_manager.queue_stats())


@app.get("/jobs/<job_id>/download")
def download_job(job_id: str):
    job = job_manager.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    if job.status != JobStatus.DONE:
        return jsonify({"error": f"Job not ready: {job.status.value}", "message": job.message}), 409
    if not job.zip_path or not Path(job.zip_path).is_file():
        return jsonify({"error": "Output file missing"}), 500
    return send_file(job.zip_path, as_attachment=True, download_name=job_download_filename(job))


def _preview_payload(preview) -> dict:
    """The record plus, once it is built, where the browser reads it from."""
    data = preview.to_dict()
    if preview.status == previews.DONE and preview.tiles_file:
        data["tiles"] = preview_tiles_url(preview.tiles_file)
    return data


@app.post("/preview")
def create_preview():
    """Queue a preview of the drawn bbox, or hand back one that already exists.

    Previews are offered only for the regions the deployment keeps current: the
    worker cuts them out of those extracts, and anything else would mean
    downloading a fresh multi-gigabyte region on a button press.
    """
    payload = request.get_json(silent=True) or {}
    try:
        west, south, east, north = parse_bbox(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    gap = regionsync.bbox_coverage_gap(west, south, east, north, GEOFABRIK_CACHE)
    if gap:
        return jsonify({"error": f"No preview here: {gap}"}), 400

    ready = previews.find_ready(west, south, east, north, previews_dir=PREVIEWS_DIR)
    if ready is not None:
        return jsonify(_preview_payload(ready)), 200

    active = previews.find_active(west, south, east, north)
    if active is not None:
        return jsonify(_preview_payload(active)), 202

    preview = previews.create(west, south, east, north, owner_id=g.client_id)
    previewqueue.enqueue(preview.preview_id)
    return jsonify(_preview_payload(preview)), 202


@app.get("/preview/<preview_id>")
def get_preview(preview_id: str):
    preview = previews.get(preview_id)
    if preview is None:
        return jsonify({"error": "Preview not found"}), 404
    if preview.status == previews.DONE and preview.tiles_file:
        if preview.is_expired():
            # Past its TTL: the file may already have been swept, and even
            # while it is still there it is a render of data and cartography
            # the deployment has moved on from. Same answer as a pruned one.
            return jsonify({"error": "This preview has expired, build it again"}), 410
        if not (PREVIEWS_DIR / preview.tiles_file).is_file():
            # Pruned away while the page was open: say so plainly instead of
            # handing the browser a URL that 404s inside MapLibre.
            return jsonify({"error": "This preview has been pruned, build it again"}), 410
    return jsonify(_preview_payload(preview))


@app.get("/vector/config")
def vector_basemap_config():
    """Tells the picker whether the vector basemap can be offered, and where from."""
    return jsonify(vector_config())


@app.get("/vector/assets/<path:asset>")
def vector_asset(asset: str):
    assets = style_dir()
    if assets is None:
        return jsonify({"error": "Vector style not installed"}), 404
    # otm_layers.json is a script that defines the layer list, so it must not be
    # served as JSON: a browser refuses to execute that.
    mimetype = "text/javascript" if asset == LAYERS_ASSET else None
    return send_from_directory(assets, asset, mimetype=mimetype)


@app.get("/health")
def health():
    try:
        require_deps()
        return jsonify({"status": "ok"})
    except RuntimeError as exc:
        return jsonify({"status": "degraded", "error": str(exc)}), 503


def prepare() -> None:
    try:
        deps = require_deps()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    previews.ensure_schema()
    job_manager.start()
    log.info("Dependencies OK (mkgmap=%s, splitter=%s)", deps.mkgmap_jar, deps.splitter_jar)


def create_app() -> Flask:
    """WSGI factory for gunicorn (Docker). Starts the Huey consumer once per worker."""
    prepare()
    return app


def main() -> None:
    prepare()
    host = os.environ.get("OTM_HOST", "0.0.0.0")
    port = int(os.environ.get("OTM_PORT", "8080"))
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
