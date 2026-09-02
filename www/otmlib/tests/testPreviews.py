"""Preview records against a real Postgres (see www/conftest.py for the fixture).

Plain pytest classes, not unittest.TestCase: the scratch database arrives as a
fixture argument, which unittest.TestCase cannot receive. Skipped without
DATABASE_URL.
"""

from __future__ import annotations

import pytest

from otmlib import previews

BBOX = (42.0, 43.0, 42.5, 43.4)


@pytest.fixture()
def store(pgDatabase):
    return previews


@pytest.fixture()
def previewsDir(tmp_path):
    directory = tmp_path / "previews"
    directory.mkdir()
    return directory


def makeDone(store, previewsDir, bbox=BBOX, name="a.pmtiles"):
    preview = store.create(*bbox)
    (previewsDir / f"{preview.preview_id}.pmtiles").write_bytes(b"pmtiles")
    return store.finish(
        preview.preview_id,
        tiles_file=f"{preview.preview_id}.pmtiles",
        minzoom=10,
        maxzoom=14,
        size_bytes=7,
    )


class TestCreate:
    def testANewPreviewStartsQueued(self, store):
        preview = store.create(*BBOX)
        assert preview.status == previews.QUEUED
        assert preview.bbox == BBOX

    def testItIsReadableBack(self, store):
        created = store.create(*BBOX)
        loaded = store.get(created.preview_id)
        assert loaded is not None
        assert loaded.preview_id == created.preview_id

    def testAnUnknownIdIsNone(self, store):
        assert store.get("nope") is None

    def testCoordinatesAreRoundedSoTheSameAreaMatches(self, store):
        preview = store.create(42.000000123, 43.0, 42.5, 43.4)
        assert preview.west == 42.0

    def testTheOwnerIsKept(self, store):
        preview = store.create(*BBOX, owner_id="client-1")
        assert store.get(preview.preview_id).owner_id == "client-1"


class TestLifecycle:
    def testStartMarksItRunning(self, store):
        preview = store.create(*BBOX)
        assert store.start(preview.preview_id).status == previews.RUNNING

    def testProgressReplacesTheMessage(self, store):
        preview = store.create(*BBOX)
        store.progress(preview.preview_id, "Вырезаю область…")
        assert store.get(preview.preview_id).message == "Вырезаю область…"

    def testFinishRecordsTheFileAndZooms(self, store, previewsDir):
        done = makeDone(store, previewsDir)
        assert done.status == previews.DONE
        assert done.tiles_file.endswith(".pmtiles")
        assert (done.minzoom, done.maxzoom) == (10, 14)
        assert done.size_bytes == 7

    def testFailKeepsTheReason(self, store):
        preview = store.create(*BBOX)
        failed = store.fail(preview.preview_id, "tilemaker exploded")
        assert failed.status == previews.ERROR
        assert failed.error == "tilemaker exploded"

    def testAFailedPreviewCanBeRetriedByStarting(self, store):
        preview = store.create(*BBOX)
        store.fail(preview.preview_id, "boom")
        restarted = store.start(preview.preview_id)
        assert restarted.status == previews.RUNNING
        assert restarted.error is None


class TestReuse:
    def testAFinishedPreviewOfTheSameAreaIsFound(self, store, previewsDir):
        done = makeDone(store, previewsDir)
        found = store.find_ready(*BBOX, previews_dir=previewsDir)
        assert found is not None and found.preview_id == done.preview_id

    def testADifferentAreaIsNotReused(self, store, previewsDir):
        makeDone(store, previewsDir)
        assert store.find_ready(10.0, 11.0, 10.5, 11.4, previews_dir=previewsDir) is None

    def testAPreviewWhoseFileIsGoneIsNotOffered(self, store, previewsDir):
        done = makeDone(store, previewsDir)
        (previewsDir / done.tiles_file).unlink()
        assert store.find_ready(*BBOX, previews_dir=previewsDir) is None

    def testAQueuedPreviewOfTheSameAreaIsFoundAsActive(self, store):
        created = store.create(*BBOX)
        found = store.find_active(*BBOX)
        assert found is not None and found.preview_id == created.preview_id

    def testARunningPreviewCountsAsActiveToo(self, store):
        created = store.create(*BBOX)
        store.start(created.preview_id)
        assert store.find_active(*BBOX) is not None

    def testAFinishedPreviewIsNotActive(self, store, previewsDir):
        makeDone(store, previewsDir)
        assert store.find_active(*BBOX) is None


class TestRecovery:
    def testRunningPreviewsGoBackToTheQueue(self, store):
        preview = store.create(*BBOX)
        store.start(preview.preview_id)
        assert store.requeue_running() == 1
        assert store.get(preview.preview_id).status == previews.QUEUED

    def testFinishedPreviewsAreLeftAlone(self, store, previewsDir):
        makeDone(store, previewsDir)
        assert store.requeue_running() == 0

    def testQueuedIdsComeBackOldestFirst(self, store):
        first = store.create(*BBOX)
        second = store.create(10.0, 11.0, 10.5, 11.4)
        assert store.queued_ids() == [first.preview_id, second.preview_id]


class TestPrune:
    def testTheNewestAreKept(self, store, previewsDir):
        kept = [makeDone(store, previewsDir, bbox=(x, 43.0, x + 0.5, 43.4)) for x in (1.0, 2.0, 3.0)]
        removed = store.prune(2, previewsDir)
        assert removed == [kept[0].preview_id]
        assert store.get(kept[0].preview_id) is None
        assert store.get(kept[2].preview_id) is not None

    def testThePrunedFileIsDeleted(self, store, previewsDir):
        old = makeDone(store, previewsDir, bbox=(1.0, 43.0, 1.5, 43.4))
        makeDone(store, previewsDir, bbox=(2.0, 43.0, 2.5, 43.4))
        store.prune(1, previewsDir)
        assert not (previewsDir / old.tiles_file).exists()

    def testAFileWithNoRowGoesToo(self, store, previewsDir):
        orphan = previewsDir / "orphan.pmtiles"
        orphan.write_bytes(b"x")
        store.prune(5, previewsDir)
        assert not orphan.exists()

    def testKeepingNothingEmptiesTheTable(self, store, previewsDir):
        makeDone(store, previewsDir)
        store.prune(0, previewsDir)
        assert store.queued_ids() == []


class TestPayload:
    def testTheDictCarriesWhatTheBrowserPolls(self, store, previewsDir):
        done = makeDone(store, previewsDir)
        data = done.to_dict()
        assert data["status"] == previews.DONE
        assert data["minzoom"] == 10
        assert data["age_seconds"] >= 0
        # The URL is added by garminsvc, which is the only side that knows
        # where nginx publishes the directory.
        assert "tiles" not in data
