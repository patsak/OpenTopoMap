-- Bbox previews: one row per "show me this area in the OTM style" request.
--
-- Shared by both services, which is why the DDL sits in otmlib rather than in
-- either service's own sql/ directory: garminsvc creates the rows and serves
-- their status over HTTP, the preview worker in the tilesvc image claims them
-- and builds the .pmtiles. The queue itself is huey's (see
-- otmlib.previewqueue) - this table is the state the browser polls.

CREATE SCHEMA IF NOT EXISTS otm;

CREATE TABLE IF NOT EXISTS otm.map_previews (
    preview_id text PRIMARY KEY,
    west double precision NOT NULL,
    south double precision NOT NULL,
    east double precision NOT NULL,
    north double precision NOT NULL,
    -- queued | running | done | error
    status text NOT NULL,
    message text NOT NULL DEFAULT '',
    error text,
    -- Basename only: the directory is otmlib.paths.previews(), mounted into
    -- nginx, and storing the path would tie a row to one deployment's layout.
    tiles_file text,
    minzoom integer NOT NULL DEFAULT 0,
    maxzoom integer NOT NULL DEFAULT 0,
    size_bytes bigint NOT NULL DEFAULT 0,
    owner_id text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS map_previews_created_idx ON otm.map_previews (created_at DESC);
-- Looked up on every request to reuse a preview of the same area instead of
-- building it again (see otmlib.previews.find_ready).
CREATE INDEX IF NOT EXISTS map_previews_bbox_idx
    ON otm.map_previews (west, south, east, north, status);
