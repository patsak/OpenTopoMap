"""Vector tile service for OpenTopoMap.

``python -m tilesvc`` keeps the configured Geofabrik extracts current: the full
PBF once, then ``.osc.gz`` diffs applied in place. Tiles are rendered from those
files per drawn bbox, on demand, by the preview worker
(``python -m tilesvc.preview``). Postgres records how far each region's
replication stream has been applied, which regions are covered, and the state of
each preview.
"""
