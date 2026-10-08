"""The data service for OpenTopoMap: the region extracts, and the files built
from them for the web app and for Overpass.

``python -m datasvc`` keeps the configured Geofabrik extracts current - the full
PBF once, then ``.osc.gz`` diffs applied in place - and then merges them into
the GOL www/overpass answers from (``otmlib.gol``). Map previews are rendered
from the same files per drawn bbox, on demand, by the preview worker
(``python -m datasvc.preview``). Postgres records how far each region's
replication stream has been applied, which regions are covered, and the state of
each preview.
"""
