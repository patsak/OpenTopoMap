// Assembles the MapLibre style of the OpenTopoMap vector map.
//
// Two pages need that style: the standalone viewer (index.html) and the bbox picker
// of the Garmin build service (garmin/static/app.js), which shows the same map the
// build will put on the device. Sources, contour thresholds and sprite wiring live
// here so the two cannot drift apart.
//
// Load after maplibre-gl.js, maplibre-contour and otm_layers.json.

var OTM_DEM = {
	url: "https://tiles.mapterhorn.com/{z}/{x}/{y}.webp",
	encoding: "terrarium",
	maxzoom: 15,
	attribution: "DEM: © mapterhorn.com",
};

// Contour intervals follow the Genshtab hierarchy: thin every 10/20 m, medium every
// 50 m, thick every 100 m. The 50/100 m split is done by the style from the
// elevation value, so only minor/major thresholds are configured here.
var OTM_CONTOUR_THRESHOLDS = {
	10: [100, 500],
	11: [50, 200],
	12: [20, 100],
	13: [20, 100],
	14: [10, 100],
	15: [10, 50],
};

var otmDemSourceInstance = null;

// One DemSource per page: it registers a protocol handler on maplibregl, and a
// second one would fight the first over the same protocol name.
function otmDemSource() {
	if (!otmDemSourceInstance) {
		otmDemSourceInstance = new mlcontour.DemSource({
			url: OTM_DEM.url,
			encoding: OTM_DEM.encoding,
			maxzoom: OTM_DEM.maxzoom,
			worker: true, // offload isoline computation to a web worker to reduce jank
			cacheSize: 100, // number of most-recent tiles to cache
			timeoutMs: 10_000, // timeout on fetch requests
		});
		otmDemSourceInstance.setupMaplibre(maplibregl);
	}
	return otmDemSourceInstance;
}

// options:
//   tiles       vector tile URL template (required)
//   maxzoom     highest zoom in the tileset, default 14
//   bounds      [w, s, e, n] coverage, keeps MapLibre from asking for missing tiles
//   attribution credit line for the vector source
//   contours    {tiles, maxzoom, attribution} for a served contour tileset; without
//               it isolines are computed in the browser from the DEM
//   sprite      absolute sprite URL, default "otm_sprite" next to the page
//   layers      layer list, default the otm_layers global from otm_layers.json
//   globe       vertical-perspective projection at low zoom, default true; must be
//               false when the map is embedded in Leaflet, which is Mercator-only
function otmVectorStyle(options) {
	var opts = options || {};
	var dem = otmDemSource();
	var vector = {
		type: "vector",
		lineMetrics: true,
		tiles: [opts.tiles],
		maxzoom: opts.maxzoom || 14,
		attribution:
			opts.attribution ||
			"Map style: © OpenTopoMap, Map data © OpenStreetMap contributors",
	};
	if (opts.bounds) {
		vector.bounds = opts.bounds;
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
			dem: {
				type: "raster-dem",
				encoding: OTM_DEM.encoding,
				tiles: [dem.sharedDemProtocolUrl],
				maxzoom: OTM_DEM.maxzoom,
				tileSize: 512,
				attribution: OTM_DEM.attribution,
			},
			"contour-source": opts.contours
				? {
						type: "vector",
						tiles: [opts.contours.tiles],
						maxzoom: opts.contours.maxzoom || 14,
						attribution: opts.contours.attribution || "Contours: © OpenTopoMap",
					}
				: // Isolines computed in the browser. They carry no on_glacier/steep
					// attributes, so glacier contours stay brown and steep slopes keep
					// their thick major lines; a served tileset from
					// tools/build_contours.py gets the full Garmin behaviour.
					{
						type: "vector",
						tiles: [
							dem.contourProtocolUrl({
								multiplier: 1,
								thresholds: OTM_CONTOUR_THRESHOLDS,
								contourLayer: "contours",
								elevationKey: "ele",
								levelKey: "level",
								extent: 4096,
								buffer: 1,
							}),
						],
						maxzoom: OTM_DEM.maxzoom,
					},
		},
		glyphs: opts.glyphs || "https://fonts.undpgeohub.org/fonts/{fontstack}/{range}.pbf",
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
