// Assembles the MapLibre style of the OpenTopoMap vector map.
//
// Its consumer is the bbox picker of the Garmin build service
// (www/garminsvc/static/app.js), which shows the same map the build will put on
// the device. Sources, contour thresholds and sprite wiring live here rather
// than in the page, so another consumer gets them for free.
//
// Load after maplibre-gl.js, maplibre-contour (mlcontour) and otm_layers.json.

// Mapterhorn terrarium tiles. MapLibre draws hillshade from them directly;
// maplibre-contour turns the same tiles into isolines in the browser.
var OTM_DEM = {
	tiles: "https://tiles.mapterhorn.com/{z}/{x}/{y}.webp",
	encoding: "terrarium",
	maxzoom: 12,
	tileSize: 512,
	attribution: "DEM: © Mapterhorn",
};

// Mirrors otmlib.constants.CONTOUR_MINZOOM / CONTOUR_MAXZOOM. Below the floor
// the ridge lines carry the relief (see the ridge-lines layer).
var OTM_CONTOURS = {
	minzoom: 12,
	maxzoom: 14,
};

// Genshtab interval on the web map: 20 m isolines, index every 100 m. maplibre-contour
// has no terrain classifier, so this is the mountain step everywhere.
function otmContourThresholds() {
	var thresholds = {};
	for (var z = OTM_CONTOURS.minzoom; z <= OTM_CONTOURS.maxzoom; z++) {
		thresholds[z] = [20, 100];
	}
	return thresholds;
}

function otmDemSource(options) {
	if (typeof mlcontour === "undefined") {
		throw new Error("otmVectorStyle: maplibre-contour (mlcontour) is required");
	}
	if (typeof maplibregl === "undefined") {
		throw new Error("otmVectorStyle: maplibre-gl is required");
	}
	var dem = options || {};
	var source = new mlcontour.DemSource({
		url: dem.tiles || OTM_DEM.tiles,
		encoding: dem.encoding || OTM_DEM.encoding,
		maxzoom: dem.maxzoom || OTM_DEM.maxzoom,
		worker: true,
	});
	source.setupMaplibre(maplibregl);
	return source;
}

// options:
//   tiles       vector tile URL template (required unless url is given)
//   url         TileJSON-or-protocol URL of the tileset, e.g.
//               "pmtiles://https://host/area.pmtiles"; wins over tiles, and is
//               what the bbox preview uses - one file, read with range requests
//   minzoom     lowest zoom in the tileset, default 0
//   maxzoom     highest zoom in the tileset, default 14
//   bounds      [w, s, e, n] coverage, keeps MapLibre from asking for missing tiles
//   attribution credit line for the vector source
//   ocean       {tiles, maxzoom} water-polygon tileset; omitted, the vector tiles
//               are used (the full worldwide profile embeds the ocean layer)
//   dem         {tiles, maxzoom, encoding, tileSize, attribution} Mapterhorn by default
//   sprite      absolute sprite URL, default "otm_sprite" next to the page
//   layers      layer list, default the otm_layers global from otm_layers.json
//   globe       vertical-perspective projection at low zoom, default true; must be
//               false when the map is embedded in Leaflet, which is Mercator-only
function otmVectorStyle(options) {
	var opts = options || {};
	if (!opts.tiles && !opts.url) {
		throw new Error("otmVectorStyle: opts.tiles or opts.url is required");
	}
	var demOpts = opts.dem || {};
	var demSource = otmDemSource(demOpts);
	var vector = {
		type: "vector",
		lineMetrics: true,
		attribution:
			opts.attribution ||
			"Map style: © OpenTopoMap, Map data © OpenStreetMap contributors",
	};
	if (opts.url) {
		// A pmtiles:// URL carries its own zoom range and bounds in the file
		// header, so MapLibre reads them from there rather than from options.
		vector.url = opts.url;
	} else {
		vector.tiles = [opts.tiles];
		vector.maxzoom = opts.maxzoom || 14;
		if (opts.minzoom) {
			vector.minzoom = opts.minzoom;
		}
		if (opts.bounds) {
			vector.bounds = opts.bounds;
		}
	}
	return {
		version: 8,
		projection:
			opts.globe === false
				? { type: "mercator" }
				: {
						type: [
							"interpolate", ["linear"], ["zoom"],
							5, "vertical-perspective",
							7, "mercator",
						],
					},
		sources: {
			"opentopomap-vector": vector,
			// Regional tilesets have no shapefile ocean; the job publishes it
			// separately. A full tileset already has the layer, so the same URL works.
			// A copy rather than `vector` itself: MapLibre resolves a source
			// spec in place, and two ids sharing one object leave the second
			// one waiting for a resolution that already happened.
			"opentopomap-ocean": opts.ocean
				? {
						type: "vector",
						tiles: [opts.ocean.tiles],
						maxzoom: opts.ocean.maxzoom || opts.maxzoom || 14,
					}
				: { ...vector },
			dem: {
				type: "raster-dem",
				encoding: demOpts.encoding || OTM_DEM.encoding,
				tiles: [demSource.sharedDemProtocolUrl],
				maxzoom: demOpts.maxzoom || OTM_DEM.maxzoom,
				tileSize: demOpts.tileSize || OTM_DEM.tileSize,
				attribution: demOpts.attribution || OTM_DEM.attribution,
			},
			"contour-source": {
				type: "vector",
				tiles: [
					demSource.contourProtocolUrl({
						thresholds: otmContourThresholds(),
						contourLayer: "contours",
						elevationKey: "ele",
						levelKey: "level",
					}),
				],
				minzoom: OTM_CONTOURS.minzoom,
				maxzoom: OTM_CONTOURS.maxzoom,
				attribution: "Contours: © Mapterhorn",
			},
		},
		glyphs: opts.glyphs || "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
		// MapLibre requires an absolute sprite URL, so resolve it against the page
		sprite: opts.sprite || new URL("otm_sprite", document.baseURI).href,
		layers: opts.layers || otm_layers,
		sky: {
			"sky-color": "#199EF3",
			"sky-horizon-blend": 0.5,
			"horizon-color": "#ffffff",
			"horizon-fog-blend": 0,
			"fog-color": "#0000ff",
			"fog-ground-blend": 0,
			"atmosphere-blend": ["interpolate", ["linear"], ["zoom"], 0, 1, 10, 1, 12, 0],
		},
		light: {
			anchor: "viewport",
			position: [3, 0, 30],
			color: "#AAAAAA",
		},
	};
}
