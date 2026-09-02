"""Tile metadata against a real Postgres (see www/conftest.py for the fixture).

Plain pytest classes, not unittest.TestCase: the scratch database arrives as a
fixture argument, which unittest.TestCase cannot receive. Skipped without
DATABASE_URL.
"""

from __future__ import annotations

import pytest

from otmlib import pgmeta


@pytest.fixture()
def meta(pgDatabase):
    return pgmeta


class TestReplicationState:
    def testAnUnknownRegionHasNoState(self, meta):
        assert meta.get_replication_state("armenia") is None

    def testTheSequenceRoundTrips(self, meta):
        meta.set_replication_state("armenia", 4893)
        assert meta.get_replication_state("armenia") == 4893

    def testASecondWriteAdvancesTheSameRow(self, meta):
        meta.set_replication_state("armenia", 1)
        meta.set_replication_state("armenia", 2)
        assert meta.get_replication_state("armenia") == 2

    def testRegionsAreTrackedIndependently(self, meta):
        meta.set_replication_state("armenia", 1)
        meta.set_replication_state("georgia", 7)
        assert meta.get_replication_state("armenia") == 1
        assert meta.get_replication_state("georgia") == 7

    def testClearingForgetsTheRegion(self, meta):
        meta.set_replication_state("armenia", 1)
        meta.clear_replication_state("armenia")
        assert meta.get_replication_state("armenia") is None


class TestRegions:
    def testCoverageIsEmptyWithNoRegions(self, meta):
        assert meta.coverage_bbox() is None
        assert meta.coverage_center() == {}

    def testCoverageSpansEveryRegion(self, meta):
        meta.upsert_region("armenia", "Armenia", (43.0, 38.0, 47.0, 42.0))
        meta.upsert_region("georgia", "Georgia", (40.0, 41.0, 47.0, 43.5))
        assert meta.coverage_bbox() == (40.0, 38.0, 47.0, 43.5)

    def testTheCenterIsLatitudeFirstForMapLibre(self, meta):
        meta.upsert_region("armenia", "Armenia", (43.0, 38.0, 47.0, 42.0))
        center = meta.coverage_center()
        assert center["center"] == [40.0, 45.0]
        assert center["zoom"] == pgmeta.DEFAULT_CENTER_ZOOM

    def testReimportingARegionMovesItsBbox(self, meta):
        meta.upsert_region("armenia", "Armenia", (43.0, 38.0, 47.0, 42.0))
        meta.upsert_region("armenia", "Armenia", (0.0, 0.0, 1.0, 1.0))
        assert meta.coverage_bbox() == (0.0, 0.0, 1.0, 1.0)

    def testPruningDropsRegionsNoLongerConfigured(self, meta):
        meta.upsert_region("armenia", "Armenia", (43.0, 38.0, 47.0, 42.0))
        meta.upsert_region("georgia", "Georgia", (40.0, 41.0, 47.0, 43.5))
        meta.prune_regions(["armenia"])
        assert meta.coverage_bbox() == (43.0, 38.0, 47.0, 42.0)

    def testPruningToNothingClearsCoverage(self, meta):
        meta.upsert_region("armenia", "Armenia", (43.0, 38.0, 47.0, 42.0))
        meta.prune_regions([])
        assert meta.coverage_bbox() is None


class TestTileState:
    def testAnUnbuiltTilesetHasNoRevision(self, meta):
        assert meta.get_tile_state("otm") is None

    def testTheRevisionRoundTrips(self, meta):
        meta.set_tile_state("otm", "armenia@1 georgia@2")
        assert meta.get_tile_state("otm") == "armenia@1 georgia@2"

    def testTilesetsAreTrackedIndependently(self, meta):
        meta.set_tile_state("otm", "a@1")
        meta.set_tile_state("otm-ocean", "shapes@1")
        assert meta.get_tile_state("otm") == "a@1"
        assert meta.get_tile_state("otm-ocean") == "shapes@1"

    def testARebuildReplacesTheRevision(self, meta):
        meta.set_tile_state("otm", "a@1")
        meta.set_tile_state("otm", "a@2")
        assert meta.get_tile_state("otm") == "a@2"
