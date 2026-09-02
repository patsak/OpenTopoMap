"""Job queue (Huey) and job records — both stored in Postgres."""

from __future__ import annotations

import json
import logging
import shutil
import threading
import traceback
import uuid
from pathlib import Path

from garminsvc.constants import JOBS_DIR
from garminsvc.job import Job, JobStatus, job_download_filename, normalize_job_name
from otmlib.proc import BuildCancelled, cancel_event
from garminsvc.retention import MAX_STORED_JOBS, cleanup_work_dir, jobs_to_keep
from garminsvc.storage import (
    allocate_family_ids,
    count_by_status,
    delete_job,
    get_job,
    list_jobs,
)

log = logging.getLogger(__name__)

__all__ = [
    "Job",
    "JobManager",
    "JobStatus",
    "MAX_STORED_JOBS",
    "job_download_filename",
    "job_manager",
    "normalize_job_name",
]


class JobManager:
    def __init__(self) -> None:
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._started = False
        self._consumer = None
        self._worker: threading.Thread | None = None
        self._running_id: str | None = None
        self._cancel_events: dict[str, threading.Event] = {}

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            from garminsvc.storage import ensure_schema

            # Both this schema and huey's own tables must exist before the
            # consumer thread starts touching them.
            ensure_schema()
            self._migrate_json_jobs()
            from garminsvc.tasks import huey

            self._consumer = huey.create_consumer(workers=1, periodic=False, worker_type="thread")
            self._started = True
        # Recover and start the consumer outside the lock: run_job() needs this lock,
        # and Huey threads must not wait for start() to finish.
        self._recover_interrupted()
        start = getattr(self._consumer, "start", None)
        if callable(start):
            start()
        else:
            self._worker = threading.Thread(
                target=self._consumer.run,
                daemon=True,
                name="huey-consumer",
            )
            self._worker.start()
        self._prune()

    def create(
        self,
        west: float,
        south: float,
        east: float,
        north: float,
        name: str = "",
        source_pbf: str | None = None,
        job_id: str | None = None,
        owner_id: str = "",
    ) -> Job:
        if not self._started:
            raise RuntimeError("JobManager is not started")
        family_id_map, family_id_contours = allocate_family_ids()
        job = Job(
            job_id=job_id or str(uuid.uuid4()),
            west=west,
            south=south,
            east=east,
            north=north,
            name=normalize_job_name(name),
            family_id_map=family_id_map,
            family_id_contours=family_id_contours,
            source_pbf=source_pbf,
            owner_id=owner_id,
        )
        job.save()
        self._enqueue(job.job_id)
        self._prune()
        return job

    def get(self, job_id: str) -> Job | None:
        return Job.load(job_id)

    def list_recent(self, limit: int = 0) -> list[Job]:
        return list_jobs(limit=0 if limit <= 0 else limit)

    def queue_stats(self) -> dict[str, int]:
        return {
            "queued": count_by_status(JobStatus.QUEUED.value),
            "running": count_by_status(JobStatus.RUNNING.value),
        }

    def cancel(self, job_id: str) -> Job | None:
        with self._lock:
            job = Job.load(job_id)
            if job is None:
                return None
            if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
                return job
            job.status = JobStatus.CANCELLED
            job.message = "Отменено"
            job.save()
            event = self._cancel_events.get(job_id)
        if event is not None:
            event.set()
        else:
            cleanup_work_dir(JOBS_DIR / job_id, keep_zip=False)
        return job

    def run_job(self, job_id: str) -> None:
        with self._lock:
            job = Job.load(job_id)
            if job is None:
                return
            if job.status in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED):
                return
            event = threading.Event()
            self._cancel_events[job_id] = event
            self._running_id = job_id
            job.status = JobStatus.RUNNING
            job.message = "Building map…"
            job.save()

        token = cancel_event.set(event)

        def log_fn(msg: str) -> None:
            job.log.append(msg)
            job.message = msg
            job.save()

        try:
            from garminsvc.builder import MapBuilder

            job_dir = JOBS_DIR / job_id
            if not job.family_id_map or not job.family_id_contours:
                job.family_id_map, job.family_id_contours = allocate_family_ids()
                job.save()
            builder = MapBuilder(job_dir, log_fn=log_fn)
            result = builder.build(
                job.west,
                job.south,
                job.east,
                job.north,
                name=job.name,
                family_id_map=job.family_id_map,
                family_id_contours=job.family_id_contours,
                source_pbf=Path(job.source_pbf) if job.source_pbf else None,
            )
            if event.is_set():
                raise BuildCancelled("cancelled")
            if result.parts:
                part_bbox = result.parts[0].bbox
                job.west, job.south, job.east, job.north = (
                    part_bbox.west,
                    part_bbox.south,
                    part_bbox.east,
                    part_bbox.north,
                )
            job.geofabrik_urls = result.geofabrik_urls
            job.parts = len(result.parts)
            job.zip_path = str(result.zip_path) if result.zip_path else None
            job.status = JobStatus.DONE
            job.message = f"Ready: {job.parts} part(s)"
        except BuildCancelled:
            job.status = JobStatus.CANCELLED
            job.message = "Отменено"
            job.log.append("Cancelled")
        except Exception as exc:  # noqa: BLE001
            if event.is_set():
                job.status = JobStatus.CANCELLED
                job.message = "Отменено"
                job.log.append("Cancelled")
            else:
                job.status = JobStatus.ERROR
                job.error = str(exc)
                job.message = str(exc)
                job.log.append(traceback.format_exc())
        finally:
            cancel_event.reset(token)
            with self._lock:
                self._cancel_events.pop(job_id, None)
                self._running_id = None
            job.save()
            keep_zip = job.status == JobStatus.DONE and bool(job.zip_path)
            cleanup_work_dir(JOBS_DIR / job_id, keep_zip=keep_zip)
            self._prune()

    def _enqueue(self, job_id: str) -> None:
        from garminsvc.tasks import build_map

        build_map(job_id)

    def _pending_job_ids(self) -> set[str]:
        from garminsvc.tasks import huey

        ids: set[str] = set()
        try:
            pending = huey.pending()
        except Exception:  # noqa: BLE001
            return ids
        for message in pending:
            args = getattr(message, "args", None) or ()
            if args:
                ids.add(str(args[0]))
        return ids

    def _recover_interrupted(self) -> None:
        pending = self._pending_job_ids()
        for job in list_jobs(limit=0):
            if job.status == JobStatus.RUNNING:
                job.status = JobStatus.QUEUED
                job.message = "Requeued after restart"
                job.save()
            if job.status == JobStatus.QUEUED and job.job_id not in pending:
                self._enqueue(job.job_id)

    def _migrate_json_jobs(self) -> None:
        for path in JOBS_DIR.glob("*/job.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                job = Job.from_dict(data)
            except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
                log.warning("Skip job.json %s: %s", path, exc)
                continue
            if get_job(job.job_id) is None:
                job.save()

    def _prune(self) -> None:
        jobs = list_jobs(limit=0)
        keep = jobs_to_keep(jobs, running_id=self._running_id)
        for job in jobs:
            if job.job_id in keep:
                continue
            log.info("Pruning job %s (%s)", job.job_id, job.status.value)
            self._purge_job(job.job_id)

        if JOBS_DIR.is_dir():
            for path in JOBS_DIR.iterdir():
                if path.is_dir() and path.name not in keep:
                    log.info("Removing orphan job dir %s", path.name)
                    shutil.rmtree(path, ignore_errors=True)

        for job_id in keep:
            if job_id == self._running_id:
                continue
            job = Job.load(job_id)
            if job is None or job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                continue
            cleanup_work_dir(
                JOBS_DIR / job_id,
                keep_zip=job.status == JobStatus.DONE and bool(job.zip_path),
            )

    @staticmethod
    def _purge_job(job_id: str) -> None:
        delete_job(job_id)
        shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)


job_manager = JobManager()
