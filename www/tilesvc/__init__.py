"""Vector tile service for OpenTopoMap.

``python -m tilesvc`` keeps the configured Geofabrik extracts current (full PBF
once, then ``.osc.gz`` diffs applied in place) and builds ``otm.mbtiles`` and
``otm-ocean.mbtiles`` with tilemaker. Martin serves those files; Postgres only
records how far each region's replication stream has been applied and which
input revision each tileset was built from.
"""
