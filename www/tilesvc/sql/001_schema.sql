-- Metadata for the tile pipeline. Nothing here is tile data.
--
-- The tiles themselves are prebuilt .mbtiles files (tilemaker, see
-- www/tilesvc/tilemaker.py) that Martin serves straight off disk. Postgres
-- only remembers what has already been done: how far each region's .osc.gz
-- replication stream has been applied to its cached PBF, which regions are in
-- the tileset and where they are, and which input revision the current
-- .mbtiles was built from. That is the whole reason a database is in this
-- pipeline at all — so no PostGIS, and no tuning for it.

CREATE SCHEMA IF NOT EXISTS otm;

-- The applied position in each region's Geofabrik replication stream. Written
-- only after osmium apply-changes has successfully rewritten the PBF, so a
-- crash mid-apply leaves this pointing at the last good state and the next run
-- re-downloads the same range instead of skipping it.
CREATE TABLE IF NOT EXISTS otm.replication_state (
    region_id text PRIMARY KEY,
    sequence_number bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Regions currently in the tileset, with the bbox of each. Plain floats rather
-- than a geometry: the only consumers are the map's initial view and the
-- tileset bounds, and both are bbox-shaped. Region polygons stay where they
-- come from (Geofabrik's index-v1.json, read through shapely).
CREATE TABLE IF NOT EXISTS otm.regions (
    region_id text PRIMARY KEY,
    name text NOT NULL,
    west double precision NOT NULL,
    south double precision NOT NULL,
    east double precision NOT NULL,
    north double precision NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- One row per built tileset ('otm', 'otm-ocean'). source_revision identifies
-- the input the file was built from — for otm, the tracked sequence of every
-- region joined together; for otm-ocean, the shapefile set's stamp. A build is
-- skipped when the revision already matches, which is what makes the nightly
-- job cheap on a day Geofabrik published nothing.
CREATE TABLE IF NOT EXISTS otm.tile_state (
    tileset text PRIMARY KEY,
    source_revision text NOT NULL,
    built_at timestamptz NOT NULL DEFAULT now()
);
