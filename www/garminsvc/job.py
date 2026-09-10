"""Job record and public JSON views. Persistence stays in storage."""

from __future__ import annotations

import re
import secrets
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

MAX_NAME_LEN = 80


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class Job:
    job_id: str
    west: float
    south: float
    east: float
    north: float
    status: JobStatus = JobStatus.QUEUED
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    message: str = ""
    log: list[str] = field(default_factory=list)
    geofabrik_urls: list[str] = field(default_factory=list)
    parts: int = 0
    zip_path: str | None = None
    error: str | None = None
    name: str = ""
    family_id_map: int = 0
    family_id_contours: int = 0
    source_pbf: str | None = None
    owner_id: str = ""

    def can_cancel(self, client_id: str) -> bool:
        if self.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
            return False
        if not self.owner_id or not client_id:
            return False
        if len(self.owner_id) != len(client_id):
            return False
        return secrets.compare_digest(self.owner_id, client_id)

    def to_dict(self, client_id: str = "") -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data.pop("owner_id", None)
        data["cancellable"] = self.can_cancel(client_id)
        return data

    def to_summary(self, client_id: str = "") -> dict[str, Any]:
        ready = self.status == JobStatus.DONE and bool(self.zip_path) and Path(self.zip_path).is_file()
        return {
            "job_id": self.job_id,
            "west": self.west,
            "south": self.south,
            "east": self.east,
            "north": self.north,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message": self.message,
            "parts": self.parts,
            "error": self.error,
            "name": self.name,
            "family_id_map": self.family_id_map,
            "family_id_contours": self.family_id_contours,
            "source_pbf": bool(self.source_pbf),
            "downloadable": ready,
            "cancellable": self.can_cancel(client_id),
        }

    def save(self) -> None:
        from garminsvc.constants import JOBS_DIR
        from garminsvc.storage import upsert_job

        self.updated_at = datetime.now(timezone.utc).isoformat()
        (JOBS_DIR / self.job_id).mkdir(parents=True, exist_ok=True)
        upsert_job(self)

    @classmethod
    def load(cls, job_id: str) -> Job | None:
        from garminsvc.storage import get_job

        return get_job(job_id)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        allowed = {item.name for item in fields(cls)}
        payload = {key: value for key, value in data.items() if key in allowed}
        payload["status"] = JobStatus(payload["status"])
        return cls(**payload)


def normalize_job_name(name: str) -> str:
    cleaned = " ".join((name or "").split())
    return cleaned[:MAX_NAME_LEN]


def job_download_filename(job: Job) -> str:
    slug = re.sub(r"[^\w\-]+", "-", job.name, flags=re.UNICODE).strip("-_")[:40]
    return f"otm-hike-{slug or job.job_id[:8]}.zip"
