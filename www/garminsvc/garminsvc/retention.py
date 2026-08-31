"""Retention: keep at most N finished maps and drop leftover work files."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Sequence
from pathlib import Path

from garminsvc.job import Job, JobStatus

log = logging.getLogger(__name__)

MAX_STORED_JOBS = 100
ZIP_NAME = "maps.zip"


def jobs_to_keep(
    jobs: Sequence[Job],
    *,
    running_id: str | None = None,
    limit: int = MAX_STORED_JOBS,
) -> set[str]:
    """Ids that must stay: active jobs plus the newest finished maps."""
    newest_ids = {job.job_id for job in jobs[:limit]}
    keep: set[str] = set()
    done_kept = 0
    for job in jobs:
        if job.status in (JobStatus.QUEUED, JobStatus.RUNNING) or job.job_id == running_id:
            keep.add(job.job_id)
        elif job.status == JobStatus.DONE and done_kept < limit:
            keep.add(job.job_id)
            done_kept += 1
        elif job.status in (JobStatus.ERROR, JobStatus.CANCELLED) and job.job_id in newest_ids:
            keep.add(job.job_id)
    return keep


def cleanup_work_dir(job_dir: Path, *, keep_zip: bool) -> None:
    if not job_dir.is_dir():
        return
    keep_names = {ZIP_NAME} if keep_zip else set()
    for path in list(job_dir.iterdir()):
        if path.name in keep_names:
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            log.warning("Failed to remove %s: %s", path, exc)
