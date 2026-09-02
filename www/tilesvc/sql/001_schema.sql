-- Metadata for the tile pipeline. Nothing here is tile data.
--
-- The tiles themselves are built per drawn bbox into a .pmtiles file
-- (tilemaker, see www/otmlib/tilemaker.py) that nginx serves straight off
-- disk. Postgres only remembers what has already been done: how far each
-- region's .osc.gz replication stream has been applied to its cached PBF, and
-- which regions are covered and where they are. That is the whole reason a
-- database is in this pipeline at all — so no PostGIS, and no tuning for it.
--
-- Dropped in the move to on-demand previews: otm.tile_state, which recorded the
-- input revision of each prebuilt tileset. An existing database keeps the table
-- until "DROP TABLE otm.tile_state" is run by hand.

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
