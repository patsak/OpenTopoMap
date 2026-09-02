"""OSM/DEM building blocks shared by the OpenTopoMap services.

garminsvc turns an extract into Garmin images, tilesvc turns the same extract into
vector tiles, and both need the identical Geofabrik sync, DEM cache, glacier index
and contour tagging. Those live here so the two services agree on the data rather
than one importing the other. The web map does not consume this DEM or glacier
tagging: it hillshades Mapterhorn in MapLibre and draws isolines in the browser.

Plain package, no install step: the services put it on sys.path (``COPY www/otmlib
/app/otmlib`` in Docker, the ``www`` directory in the test config).
"""
