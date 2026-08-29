import tempfile
import unittest
from pathlib import Path

from tests.jobaide import job
from mapsvc.job import JobStatus
from mapsvc.retention import MAX_STORED_JOBS, cleanup_work_dir, jobs_to_keep


class TestJobsToKeep(unittest.TestCase):
    def testKeepsAtMostLimitDoneMaps(self):
        done = [
            job(job_id=f"done-{i:03d}", status=JobStatus.DONE, created_at=f"2026-01-01T00:{i:02d}:00+00:00")
            for i in range(MAX_STORED_JOBS + 5)
        ]
        # Newest first, same order as storage.list_jobs.
        done.reverse()
        keep = jobs_to_keep(done, limit=MAX_STORED_JOBS)
        self.assertEqual(len(keep), MAX_STORED_JOBS)
        self.assertIn("done-104", keep)
        self.assertNotIn("done-000", keep)

    def testAlwaysKeepsQueuedAndRunning(self):
        records = [
            job(job_id="run", status=JobStatus.RUNNING),
            job(job_id="wait", status=JobStatus.QUEUED),
            job(job_id="old", status=JobStatus.DONE),
        ]
        keep = jobs_to_keep(records, limit=1)
        self.assertEqual(keep, {"run", "wait", "old"})

    def testDropsOldErrorsOutsideNewestWindow(self):
        records = [
            job(job_id="new-err", status=JobStatus.ERROR),
            job(job_id="done", status=JobStatus.DONE),
            job(job_id="old-err", status=JobStatus.ERROR),
        ]
        keep = jobs_to_keep(records, limit=2)
        self.assertIn("new-err", keep)
        self.assertIn("done", keep)
        self.assertNotIn("old-err", keep)


class TestCleanupWorkDir(unittest.TestCase):
    def testKeepsOnlyZipWhenRequested(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            (job_dir / "build").mkdir()
            (job_dir / "build" / "tmp.pbf").write_bytes(b"x")
            (job_dir / "artifacts").mkdir()
            (job_dir / "maps.zip").write_bytes(b"zip")
            (job_dir / "upload.osm").write_text("osm")
            cleanup_work_dir(job_dir, keep_zip=True)
            names = {path.name for path in job_dir.iterdir()}
            self.assertEqual(names, {"maps.zip"})

    def testRemovesEverythingIncludingZip(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            (job_dir / "build").mkdir()
            (job_dir / "maps.zip").write_bytes(b"zip")
            cleanup_work_dir(job_dir, keep_zip=False)
            self.assertEqual(list(job_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
