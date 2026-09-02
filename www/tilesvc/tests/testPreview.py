"""The preview worker: one queued bbox to one published .pmtiles.

Postgres is real (the record is the thing being advanced), osmium and tilemaker
are not - what matters here is which file ends up published under which name,
and what the row says when a step fails. Skipped without DATABASE_URL.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from shapely.geometry import box

from otmlib import previews
from tilesvc import config as tilesvc_config
from tilesvc import preview

BBOX = (42.0, 43.0, 42.5, 43.4)
REGION_CONFIG = (
    Path(__file__).resolve().parents[3] / "vector/tilemaker" / preview.CONFIG_SOURCE
)


def _configFacts(derived: Path, styles: Path) -> dict:
    """What the derived config says, next to what it was derived from."""
    import json

    config = json.loads(derived.read_text(encoding="utf-8"))
    source = json.loads((styles / preview.CONFIG_SOURCE).read_text(encoding="utf-8"))
    return {
        "minzoom": config["settings"]["minzoom"],
        "maxzoom": config["settings"]["maxzoom"],
        "layers": sorted(config["layers"]),
        "source_layers": sorted(source["layers"]),
    }


@pytest.fixture()
def cfg(tmp_path, pgDatabase):
    return tilesvc_config.Config(
        data_dir=tmp_path,
        regions=[tilesvc_config.Region(geofabrik_id="russia/north-caucasus-fed-district")],
    )


@pytest.fixture()
def region():
    item = mock.Mock()
    item.region_id = "russia/north-caucasus-fed-district"
    item.name = "North Caucasus"
    item.geometry = box(40.0, 41.0, 48.0, 46.0)
    return item


@pytest.fixture()
def worker(cfg, region, tmp_path):
    """The worker with its two external steps stubbed out.

    ``built`` collects what tilemaker was asked to produce, so a test can assert
    on the command's shape without running it.
    """
    pbf = tmp_path / "region.osm.pbf"
    pbf.write_bytes(b"pbf")
    styles = tmp_path / "styles"
    styles.mkdir()
    (styles / preview.CONFIG_SOURCE).write_text(
        REGION_CONFIG.read_text(encoding="utf-8"), encoding="utf-8"
    )
    sync = mock.Mock(return_value=[mock.Mock(pbf=pbf)])
    built: list[dict] = []

    def fakeBuild(**kwargs):
        # Read the derived config while it still exists: the work directory it
        # lives in is cleaned up as soon as the build returns.
        kwargs["settings"] = _configFacts(Path(kwargs["config"]), styles)
        built.append(kwargs)
        Path(kwargs["output"]).write_bytes(b"pmtiles" * 100)
        return kwargs["output"]

    patches = [
        mock.patch.object(preview.regionsync, "configured_regions", return_value=[region]),
        mock.patch.object(preview.regionsync, "sync_regions", sync),
        mock.patch.object(
            preview, "extract_bbox", side_effect=lambda pbfs, w, s, e, n, out: out
        ),
        mock.patch.object(preview.runner, "build", side_effect=fakeBuild),
        mock.patch.object(preview.tilemaker, "style_dir", return_value=styles),
    ]
    for patch in patches:
        patch.start()
    yield mock.Mock(cfg=cfg, sync=sync, built=built, dir=preview.previews_dir(cfg))
    for patch in patches:
        patch.stop()


class TestBuild:
    def testAQueuedPreviewEndsUpDone(self, worker):
        row = previews.create(*BBOX)
        preview.build(row.preview_id, worker.cfg)
        done = previews.get(row.preview_id)
        assert done.status == previews.DONE
        assert done.tiles_file == f"{row.preview_id}.pmtiles"

    def testItBuildsIntoAPmtilesName(self, worker):
        # tilemaker picks its output format from the extension: a staging name
        # like "x.pmtiles.tmp" makes it write a directory of loose tiles, and
        # the preview then never appears.
        row = previews.create(*BBOX)
        preview.build(row.preview_id, worker.cfg)
        assert Path(worker.built[0]["output"]).suffix == ".pmtiles"

    def testTheFileIsPublishedUnderTheFinalName(self, worker):
        row = previews.create(*BBOX)
        preview.build(row.preview_id, worker.cfg)
        assert (worker.dir / f"{row.preview_id}.pmtiles").is_file()
        # Published by rename: the half-written name must not survive.
        assert not (worker.dir / f"{row.preview_id}.pmtiles.tmp").exists()

    def testTheRecordedSizeIsTheFileSize(self, worker):
        row = previews.create(*BBOX)
        preview.build(row.preview_id, worker.cfg)
        done = previews.get(row.preview_id)
        assert done.size_bytes == (worker.dir / done.tiles_file).stat().st_size

    def testTheZoomRangeIsTheOneItBuilt(self, worker):
        row = previews.create(*BBOX)
        preview.build(row.preview_id, worker.cfg)
        done = previews.get(row.preview_id)
        assert (done.minzoom, done.maxzoom) == (preview.MINZOOM, preview.MAXZOOM)
        settings = worker.built[0]["settings"]
        assert (settings["minzoom"], settings["maxzoom"]) == (preview.MINZOOM, preview.MAXZOOM)

    def testItBuildsTheSameLayersAsTheServedTileset(self, worker):
        row = previews.create(*BBOX)
        preview.build(row.preview_id, worker.cfg)
        # The config is derived, so a layer added to the map cannot be
        # forgotten here - only the zoom range differs.
        assert worker.built[0]["config"].name == "tilemaker-config-preview.json"
        assert worker.built[0]["settings"]["layers"] == worker.built[0]["settings"]["source_layers"]

    def testTheRegionsAreSyncedFirst(self, worker):
        row = previews.create(*BBOX)
        preview.build(row.preview_id, worker.cfg)
        assert worker.sync.call_count == 1

    def testTheWorkDirectoryIsCleanedUp(self, worker):
        row = previews.create(*BBOX)
        preview.build(row.preview_id, worker.cfg)
        assert not (worker.dir / f"{row.preview_id}.work").exists()

    def testProgressIsVisibleWhileItRuns(self, worker):
        row = previews.create(*BBOX)
        seen = []
        real = previews.progress

        def spy(preview_id, message):
            seen.append(message)
            real(preview_id, message)

        with mock.patch.object(preview.previews, "progress", side_effect=spy):
            preview.build(row.preview_id, worker.cfg)
        assert any("tilemaker" in message for message in seen)


class TestFailure:
    def testATilemakerFailureLandsInTheRecord(self, worker):
        row = previews.create(*BBOX)
        with mock.patch.object(preview.runner, "build", side_effect=RuntimeError("boom")):
            preview.build(row.preview_id, worker.cfg)
        failed = previews.get(row.preview_id)
        assert failed.status == previews.ERROR
        assert "boom" in failed.error

    def testNoOutputIsLeftBehindAfterAFailure(self, worker):
        row = previews.create(*BBOX)
        with mock.patch.object(preview.runner, "build", side_effect=RuntimeError("boom")):
            preview.build(row.preview_id, worker.cfg)
        assert list(worker.dir.glob("*.pmtiles")) == []
        assert list(worker.dir.glob(f"{row.preview_id}*")) == []

    def testAnEmptyOutputCountsAsAFailure(self, worker):
        row = previews.create(*BBOX)
        with mock.patch.object(
            preview.runner, "build", side_effect=lambda **kw: Path(kw["output"]).touch()
        ):
            preview.build(row.preview_id, worker.cfg)
        assert previews.get(row.preview_id).status == previews.ERROR

    def testABboxOutsideTheConfiguredRegionsFails(self, worker):
        row = previews.create(10.0, 50.0, 10.5, 50.4)
        preview.build(row.preview_id, worker.cfg)
        failed = previews.get(row.preview_id)
        assert failed.status == previews.ERROR
        assert "configured regions" in failed.error

    def testAVanishedPreviewIsNotAnError(self, worker):
        preview.build("no-such-preview", worker.cfg)

    def testAnAlreadyBuiltPreviewIsNotRebuilt(self, worker):
        row = previews.create(*BBOX)
        preview.build(row.preview_id, worker.cfg)
        preview.build(row.preview_id, worker.cfg)
        assert len(worker.built) == 1


class TestRetention:
    def testOnlyTheNewestPreviewsSurvive(self, worker):
        with mock.patch.object(preview, "KEEP_PREVIEWS", 2):
            rows = []
            for index in range(3):
                row = previews.create(42.0 + index, 43.0, 42.5 + index, 43.4)
                preview.build(row.preview_id, worker.cfg)
                rows.append(row)
        assert previews.get(rows[0].preview_id) is None
        assert not (worker.dir / f"{rows[0].preview_id}.pmtiles").exists()
        assert previews.get(rows[2].preview_id) is not None


class TestRecover:
    def testInterruptedPreviewsAreQueuedAgain(self, cfg):
        row = previews.create(*BBOX)
        previews.start(row.preview_id)
        with mock.patch.object(preview.previewqueue, "enqueue") as enqueue:
            preview.recover()
        assert previews.get(row.preview_id).status == previews.QUEUED
        assert enqueue.call_args_list == [mock.call(row.preview_id)]

    def testQueuedPreviewsAreReenqueued(self, cfg):
        row = previews.create(*BBOX)
        with mock.patch.object(preview.previewqueue, "enqueue") as enqueue:
            preview.recover()
        assert enqueue.call_args_list == [mock.call(row.preview_id)]

    def testNothingToRecoverEnqueuesNothing(self, cfg):
        with mock.patch.object(preview.previewqueue, "enqueue") as enqueue:
            preview.recover()
        assert enqueue.call_args_list == []
