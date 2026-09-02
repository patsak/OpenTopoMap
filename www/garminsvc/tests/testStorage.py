"""Job storage against a real Postgres (see ../../conftest.py for the fixture).

Written as plain pytest classes rather than unittest.TestCase like the rest of
the suite, because unittest.TestCase cannot receive fixture arguments and the
scratch database arrives as one. Skipped entirely without DATABASE_URL.
"""

from __future__ import annotations

import pytest

from garminsvc import storage
from garminsvc.constants import FAMILY_ID_CONTOURS, FAMILY_ID_MAP
from garminsvc.job import Job, JobStatus


def job(jobId="j1", **overrides):
    fields = dict(
        job_id=jobId,
        west=43.0,
        south=42.0,
        east=44.0,
        north=43.0,
        name="Кавказ",
        owner_id="owner-1",
    )
    fields.update(overrides)
    return Job(**fields)


@pytest.fixture()
def store(pgDatabase):
    storage.ensure_schema()
    return storage


class TestUpsert:
    def testAJobRoundTrips(self, store):
        original = job(log=["a", "b"], geofabrik_urls=["https://x/a.osm.pbf"], parts=1)
        store.upsert_job(original)
        loaded = store.get_job("j1")
        assert loaded.job_id == "j1"
        assert loaded.name == "Кавказ"
        assert loaded.west == 43.0
        assert loaded.log == ["a", "b"]
        assert loaded.geofabrik_urls == ["https://x/a.osm.pbf"]
        assert loaded.status == JobStatus.QUEUED
        assert loaded.owner_id == "owner-1"

    def testAMissingJobIsNone(self, store):
        assert store.get_job("nope") is None

    def testASecondSaveUpdatesInPlace(self, store):
        store.upsert_job(job())
        updated = job(status=JobStatus.DONE, message="готово", parts=2, zip_path="/tmp/maps.zip")
        store.upsert_job(updated)
        loaded = store.get_job("j1")
        assert loaded.status == JobStatus.DONE
        assert loaded.message == "готово"
        assert loaded.parts == 2
        assert loaded.zip_path == "/tmp/maps.zip"
        assert len(store.list_jobs()) == 1

    def testOwnershipIsWriteOnce(self, store):
        """A save from the worker must not be able to reassign the job."""
        store.upsert_job(job(owner_id="owner-1"))
        store.upsert_job(job(owner_id="attacker", status=JobStatus.RUNNING))
        loaded = store.get_job("j1")
        assert loaded.owner_id == "owner-1"
        assert loaded.status == JobStatus.RUNNING

    def testEmptyListsSurviveTheRoundTrip(self, store):
        store.upsert_job(job())
        loaded = store.get_job("j1")
        assert loaded.log == []
        assert loaded.geofabrik_urls == []


class TestListing:
    def testJobsComeBackNewestFirst(self, store):
        """retention.jobs_to_keep assumes this order."""
        store.upsert_job(job("old", created_at="2026-01-01T00:00:00+00:00"))
        store.upsert_job(job("new", created_at="2026-02-01T00:00:00+00:00"))
        store.upsert_job(job("mid", created_at="2026-01-15T00:00:00+00:00"))
        assert [j.job_id for j in store.list_jobs()] == ["new", "mid", "old"]

    def testTheLimitKeepsTheNewest(self, store):
        store.upsert_job(job("old", created_at="2026-01-01T00:00:00+00:00"))
        store.upsert_job(job("new", created_at="2026-02-01T00:00:00+00:00"))
        assert [j.job_id for j in store.list_jobs(limit=1)] == ["new"]

    def testCountByStatus(self, store):
        store.upsert_job(job("a", status=JobStatus.QUEUED))
        store.upsert_job(job("b", status=JobStatus.QUEUED))
        store.upsert_job(job("c", status=JobStatus.DONE))
        assert store.count_by_status("queued") == 2
        assert store.count_by_status("done") == 1
        assert store.count_by_status("error") == 0

    def testDelete(self, store):
        store.upsert_job(job())
        store.delete_job("j1")
        assert store.get_job("j1") is None

    def testDeletingATwiceIsNotAnError(self, store):
        store.delete_job("j1")
        store.delete_job("j1")


class TestFamilyIds:
    def testTheFirstPairStartsAtTheConfiguredBases(self, store):
        assert store.allocate_family_ids() == (FAMILY_ID_MAP, FAMILY_ID_CONTOURS)

    def testEachAllocationIsDistinct(self, store):
        first = store.allocate_family_ids()
        second = store.allocate_family_ids()
        assert first != second
        assert second[0] == first[0] + 1
        assert second[1] == first[1] + 1

    def testAnIdInUseByAJobIsSkipped(self, store):
        """Two live maps sharing a family-id is what a device rejects."""
        store.upsert_job(job(family_id_map=FAMILY_ID_MAP, family_id_contours=FAMILY_ID_CONTOURS))
        mapId, contoursId = store.allocate_family_ids()
        assert mapId != FAMILY_ID_MAP
        assert contoursId != FAMILY_ID_CONTOURS

    def testTheCursorSurvivesADeletedJob(self, store):
        first = store.allocate_family_ids()
        store.upsert_job(job(family_id_map=first[0], family_id_contours=first[1]))
        store.delete_job("j1")
        second = store.allocate_family_ids()
        assert second[0] > first[0]


class TestConnectionReuse:
    """The connection is per-thread and long-lived, so a failed statement must
    not leave it in "current transaction is aborted" for everything after."""

    def testTheSameConnectionServesRepeatedCalls(self, store):
        store.upsert_job(job())
        first = store.connect()
        store.get_job("j1")
        assert store.connect() is first

    def testAFailedStatementDoesNotPoisonTheConnection(self, store):
        import psycopg

        with pytest.raises(psycopg.Error):
            with store._session() as conn:
                conn.execute("SELECT * FROM otm_garmin.no_such_table")
        store.upsert_job(job())
        assert store.get_job("j1").job_id == "j1"

    def testAClosedConnectionIsReplaced(self, store):
        store.connect().close()
        store.upsert_job(job())
        assert store.get_job("j1") is not None
