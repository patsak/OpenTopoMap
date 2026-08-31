import unittest
from uuid import uuid4

from tests.jobaide import job
from garminsvc.job import JobStatus, job_download_filename, normalize_job_name


class TestJobCancel(unittest.TestCase):
    def testOwnerCanCancelQueuedJob(self):
        owner = str(uuid4())
        record = job(status=JobStatus.QUEUED, owner_id=owner)
        self.assertTrue(record.can_cancel(owner))

    def testStrangerCannotCancel(self):
        record = job(status=JobStatus.RUNNING, owner_id=str(uuid4()))
        self.assertFalse(record.can_cancel(str(uuid4())))

    def testFinishedJobIsNotCancellable(self):
        owner = str(uuid4())
        record = job(status=JobStatus.DONE, owner_id=owner)
        self.assertFalse(record.can_cancel(owner))

    def testEmptyOwnerCannotCancel(self):
        record = job(status=JobStatus.QUEUED, owner_id="")
        self.assertFalse(record.can_cancel(str(uuid4())))

    def testSummaryHidesOwnerAndExposesCancellable(self):
        owner = str(uuid4())
        record = job(status=JobStatus.QUEUED, owner_id=owner)
        summary = record.to_summary(owner)
        self.assertNotIn("owner_id", summary)
        self.assertTrue(summary["cancellable"])
        self.assertFalse(record.to_summary(str(uuid4()))["cancellable"])


class TestJobName(unittest.TestCase):
    def testNormalizeCollapsesWhitespaceAndCutsLength(self):
        self.assertEqual(normalize_job_name("  Эльбрус   запад  "), "Эльбрус запад")
        self.assertEqual(len(normalize_job_name("x" * 200)), 80)

    def testDownloadFilenameUsesSlug(self):
        record = job(job_id="abcd1234-eeee-ffff-0000-111111111111", name="Эльбрус / запад")
        self.assertTrue(job_download_filename(record).startswith("otm-hike-"))
        self.assertTrue(job_download_filename(record).endswith(".zip"))


if __name__ == "__main__":
    unittest.main()
