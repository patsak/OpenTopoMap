"""Test data for Job records — real objects, not mocks."""

from __future__ import annotations

from uuid import uuid4

from garminsvc.job import Job, JobStatus


def job(**overrides) -> Job:
    payload = {
        "job_id": str(uuid4()),
        "west": 42.0,
        "south": 43.0,
        "east": 42.5,
        "north": 43.4,
        "status": JobStatus.QUEUED,
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-01T00:00:00+00:00",
        "name": "Test map",
        "owner_id": str(uuid4()),
    }
    payload.update(overrides)
    return Job(**payload)
