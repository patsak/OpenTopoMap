#!/usr/bin/env python3
"""HTTP service for building OpenTopoMap Garmin hike maps by bbox.

Prerequisites (install separately, service will refuse to start without them):

  pip install -r requirements-server.txt
  python download_deps.py
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from flask import Flask, g, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import RequestEntityTooLarge

from mapsvc.bbox import parse_bbox
from mapsvc.client import CLIENT_COOKIE, CLIENT_COOKIE_MAX_AGE, resolve_client_id
from mapsvc.constants import JOBS_DIR, MAX_UPLOAD_BYTES
from mapsvc.deps import download_deps, require_deps, sea_bounds_ready
from mapsvc.job import JobStatus, job_download_filename, normalize_job_name
from mapsvc.jobs import job_manager
from mapsvc.osmfile import UploadError, normalize_upload_name, save_upload_stream

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
    return jsonify({"error": f"Файл больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ"}), 413


@app.post("/maps/upload")
def create_map_upload():
    name = normalize_job_name(str(request.form.get("name") or ""))
    if not name:
        return jsonify({"error": "Укажите название карты"}), 400
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "Нужен файл .osm или .osm.pbf"}), 400
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
        return jsonify({"error": f"Нельзя отменить: {job.status.value}"}), 409
    if not job.can_cancel(g.client_id):
        return jsonify({"error": "Отменить сборку может только тот, кто её запустил"}), 403
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


@app.get("/health")
def health():
    try:
        require_deps()
        return jsonify({"status": "ok"})
    except RuntimeError as exc:
        return jsonify({"status": "degraded", "error": str(exc)}), 503


def prepare() -> None:
    try:
        if not sea_bounds_ready():
            log.info("First run: downloading sea/bounds into data/ (progress below)")
        deps = download_deps(log=log.info)
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

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
