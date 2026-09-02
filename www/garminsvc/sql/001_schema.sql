-- Garmin build-service job records.
--
-- Own schema, same database as the tile metadata (www/tilesvc/sql/001_schema.sql):
-- one Postgres for both services, and huey's own huey_* tables land in public.
-- Applied by otmlib.pg.ensure_schema() on service start.

CREATE SCHEMA IF NOT EXISTS otm_garmin;

CREATE TABLE IF NOT EXISTS otm_garmin.jobs (
    job_id text PRIMARY KEY,
    name text NOT NULL DEFAULT '',
    west double precision NOT NULL,
    south double precision NOT NULL,
    east double precision NOT NULL,
    north double precision NOT NULL,
    status text NOT NULL,
    -- ISO-8601 strings rather than timestamptz: these values are produced and
    -- compared as strings throughout (Job.created_at, the API payload, and
    -- retention's newest-first ordering), and ISO-8601 sorts the same either
    -- way. Storing them as timestamps would only add a conversion at both ends.
    created_at text NOT NULL,
    updated_at text NOT NULL,
    message text NOT NULL DEFAULT '',
    log jsonb NOT NULL DEFAULT '[]'::jsonb,
    geofabrik_urls jsonb NOT NULL DEFAULT '[]'::jsonb,
    parts integer NOT NULL DEFAULT 0,
    zip_path text,
    error text,
    family_id_map integer NOT NULL DEFAULT 0,
    family_id_contours integer NOT NULL DEFAULT 0,
    source_pbf text,
    owner_id text NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS jobs_created_idx ON otm_garmin.jobs (created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON otm_garmin.jobs (status);

-- Where the next Garmin family-id search starts, per kind. The ids themselves
-- must be unique across live jobs (a device refuses two maps sharing one), so
-- allocate_family_ids() checks the jobs table too and this is only a cursor.
CREATE TABLE IF NOT EXISTS otm_garmin.family_id_seq (
    name text PRIMARY KEY,
    value integer NOT NULL
);
