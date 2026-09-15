(() => {
  const JOB_STORAGE_KEY = "otm-garmin-current-job";
  const BASEMAP_STORAGE_KEY = "otm-garmin-basemap";
  const OSM_ATTR =
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';
  // The built preview of the drawn bbox, offered in the same dropdown as the
  // public maps: it is a whole map (the OTM style has its own background), not
  // something that can sit on top of one.
  const PREVIEW_BASEMAP_ID = "otm-preview";
  const MAPLIBRE_CSS = "https://cdn.jsdelivr.net/npm/maplibre-gl@5/dist/maplibre-gl.css";
  // Pinned to 5: maplibre-gl 6.x no longer ships dist/maplibre-gl.js, and
  // hillshade-method needs 5 or newer anyway.
  const VECTOR_LIBS = [
    "https://cdn.jsdelivr.net/npm/maplibre-gl@5/dist/maplibre-gl.js",
    "https://cdn.jsdelivr.net/npm/maplibre-contour@0.1.0/dist/index.min.js",
    "https://cdn.jsdelivr.net/npm/@maplibre/maplibre-gl-leaflet@0.1.4/leaflet-maplibre-gl.js",
    "https://cdn.jsdelivr.net/npm/pmtiles@4.4.0/dist/pmtiles.js",
  ];
  const BASEMAPS = [
    {
      id: "osm",
      name: "OSM Standard",
      url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
      maxZoom: 19,
      attribution: OSM_ATTR,
    },
    {
      id: "osm-de",
      name: "OSM Germany",
      url: "https://tile.openstreetmap.de/{z}/{x}/{y}.png",
      maxZoom: 18,
      attribution: `${OSM_ATTR} &amp; <a href="https://www.openstreetmap.de/">FOSSGIS</a>`,
    },
    {
      id: "osm-fr",
      name: "OSM France",
      url: "https://{s}.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png",
      maxZoom: 20,
      attribution: `${OSM_ATTR} &amp; <a href="https://www.openstreetmap.fr/">OSM France</a>`,
    },
    {
      id: "osm-hot",
      name: "OSM Humanitarian",
      url: "https://{s}.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png",
      maxZoom: 19,
      attribution: `${OSM_ATTR}, <a href="https://www.hotosm.org/">HOT</a> style hosted by OSM France`,
    },
    {
      id: "otm",
      name: "OpenTopoMap",
      url: "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
      maxZoom: 17,
      attribution: `${OSM_ATTR}, <a href="https://viewfinderpanoramas.org">SRTM</a> | style: <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)`,
    },
    {
      id: "cyclosm",
      name: "CyclOSM",
      url: "https://{s}.tile-cyclosm.openstreetmap.fr/cyclosm/{z}/{x}/{y}.png",
      maxZoom: 20,
      attribution: `${OSM_ATTR} | <a href="https://www.cyclosm.org">CyclOSM</a> hosted by OSM France`,
    },
  ];
  const STATUS_LABEL = {
    queued: "queued",
    running: "building",
    done: "done",
    error: "failed",
    cancelled: "cancelled",
  };
  const MAX_BBOX_SIDE_KM = 500;
  // Outlines of the regions the service has already downloaded (/regions):
  // the area a preview or a build can be cut out of. Background, not a control
  // - a thin dashed line and barely any fill, in a pane of its own below the
  // drawn rectangle, so it never gets in the way of the draw tool.
  const REGION_STYLE = {
    color: "#3f6f9f",
    weight: 1.5,
    dashArray: "6 4",
    fillColor: "#3f6f9f",
    fillOpacity: 0.06,
  };
  const REGIONS_PANE = "otm-regions";
  const MAX_UPLOAD_BYTES = 200 * 1024 * 1024;

  // Leaflet/MapLibre instances, timers and the raw upload File live outside the
  // Alpine component: Alpine wraps x-data in a reactive Proxy, and neither a
  // Leaflet map (huge circular object graph) nor a File (throws on proxied
  // internal-slot access) belongs behind one. Only plain, template-facing
  // state lives on the object garminApp() returns.
  let map = null;
  let baseLayer = null;
  let drawnItems = null;
  let drawnLayer = null;
  let pollTimer = null;
  let previewTimer = null;
  let vectorLibs = null;
  let otmLayers = null; // parsed otm_layers.json, filled in by ensureVectorLibs
  let styleSpec = null; // /vector/config — MapLibre style assets and the DEM
  let previewSpec = null; // the built preview: {preview_id, tiles, …}
  let regionsLayer = null; // /regions, the covered area drawn on the map
  let osmFile = null;
  let initialView = null; // the view (if any) carried in the URL hash at load

  // URL hash keeps the current view shareable/bookmarkable: #zoom/lat/lon.
  function readHash() {
    const match = /^#(\d{1,2}(?:\.\d+)?)\/(-?\d+(?:\.\d+)?)\/(-?\d+(?:\.\d+)?)$/.exec(location.hash);
    if (!match) return null;
    const [, zoomStr, latStr, lonStr] = match;
    const view = { zoom: Number(zoomStr), lat: Number(latStr), lon: Number(lonStr) };
    if (Math.abs(view.lat) > 90 || Math.abs(view.lon) > 180) return null;
    return view;
  }

  // Accepts "lat, lon" (the usual copy-paste order) but also falls back to
  // "lon, lat" when only that order is a valid lat/lon pair.
  function parseCoordinates(text) {
    const parts = text.split(/[,;\s]+/).map((s) => s.trim()).filter(Boolean).map(Number);
    if (parts.length !== 2 || parts.some(Number.isNaN)) return null;
    const [a, b] = parts;
    let lat = a;
    let lon = b;
    if (Math.abs(lat) > 90 || Math.abs(lon) > 180) {
      lat = b;
      lon = a;
    }
    if (Math.abs(lat) > 90 || Math.abs(lon) > 180) return null;
    return { lat, lon };
  }

  function fmt(n) {
    return Number(n).toFixed(5);
  }

  function fmtDate(iso) {
    if (!iso) return "";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    return date.toLocaleString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  // A built preview is reused for the same rectangle only until its TTL is up,
  // after which the button builds it again - so the status line says how long
  // this one still counts as current.
  function fmtPreviewTtl(seconds) {
    const left = Number(seconds);
    if (!Number.isFinite(left) || left <= 0) return "";
    if (left >= 3600) return ` · current for ${Math.round(left / 3600)} h`;
    return ` · current for ${Math.max(1, Math.round(left / 60))} min`;
  }

  function bboxMaxSideKm(b) {
    const sw = L.latLng(b.south, b.west);
    const se = L.latLng(b.south, b.east);
    const nw = L.latLng(b.north, b.west);
    const ne = L.latLng(b.north, b.east);
    return (
      Math.max(sw.distanceTo(se), nw.distanceTo(ne), sw.distanceTo(nw), se.distanceTo(ne)) /
      1000
    );
  }

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = src;
      script.onload = () => resolve();
      script.onerror = () => reject(new Error(`failed to load ${src}`));
      document.head.append(script);
    });
  }

  // MapLibre gets the style as an object, so it has no base URL to resolve our own
  // paths against; URL() is no help either because it would escape the {z}/{x}/{y}
  // braces of a tile template.
  function absoluteUrl(url) {
    return url.startsWith("/") ? window.location.origin + url : url;
  }

  function ensureVectorLibs(spec) {
    if (!vectorLibs) {
      const css = document.createElement("link");
      css.rel = "stylesheet";
      css.href = MAPLIBRE_CSS;
      document.head.append(css);
      vectorLibs = (async () => {
        for (const src of [...VECTOR_LIBS, spec.style]) {
          await loadScript(src);
        }
        // otm_layers.json is plain JSON, not a script — fetched and parsed
        // rather than loaded as a <script> tag.
        otmLayers = await (await fetch(spec.layers)).json();
        // A preview is one .pmtiles file read with range requests, so the
        // protocol has to exist before a style may name one. metadata:true is
        // what makes a pmtiles:// URL answer with TileJSON (layer list, zoom
        // range, coverage) instead of tiles alone.
        maplibregl.addProtocol("pmtiles", new pmtiles.Protocol({ metadata: true }).tile);
      })();
      vectorLibs.catch(() => {
        vectorLibs = null; // let the next attempt retry the download
      });
    }
    return vectorLibs;
  }

  async function previewBaseLayer(spec, preview) {
    await ensureVectorLibs(spec);
    return L.maplibreGL({
      attribution: spec.attribution,
      style: otmVectorStyle({
        // Zoom range and coverage come out of the file header, so MapLibre
        // asks for nothing outside the area that was built. The service hands
        // out a same-origin path now that one nginx serves both the page and
        // the previews; pmtiles fetches whatever follows the scheme, so it has
        // to be absolute by the time it gets there.
        url: `pmtiles://${absoluteUrl(preview.tiles)}`,
        attribution: spec.attribution,
        dem: spec.dem ? { ...spec.dem, tiles: absoluteUrl(spec.dem.tiles) } : undefined,
        sprite: absoluteUrl(spec.sprite),
        layers: otmLayers,
        globe: false, // Leaflet only knows Mercator
      }),
    });
  }

  function loadCurrentJob() {
    try {
      const raw = localStorage.getItem(JOB_STORAGE_KEY);
      if (!raw) return null;
      const data = JSON.parse(raw);
      if (!data || typeof data.jobId !== "string") return null;
      return data;
    } catch {
      return null;
    }
  }

  const GotoControl = L.Control.extend({
    options: { position: "topright" },
    onAdd() {
      const container = L.DomUtil.create("div", "goto-control leaflet-bar");
      const toggle = L.DomUtil.create("button", "goto-toggle", container);
      toggle.type = "button";
      toggle.title = "Go to coordinates";
      toggle.setAttribute("aria-label", "Go to coordinates");
      toggle.innerHTML =
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7zm0 9.5A2.5 2.5 0 1 1 12 6.5a2.5 2.5 0 0 1 0 5z"/></svg>';

      const panel = L.DomUtil.create("div", "goto-panel", container);
      const input = L.DomUtil.create("input", "", panel);
      input.type = "text";
      input.inputMode = "decimal";
      input.placeholder = "55.7558, 37.6173";
      input.autocomplete = "off";
      const error = L.DomUtil.create("div", "goto-error", panel);
      error.hidden = true;
      const hint = L.DomUtil.create("div", "goto-hint", panel);
      hint.textContent = "Latitude, longitude (or the other way round — the order is worked out)";
      const goBtn = L.DomUtil.create("button", "btn primary goto-go", panel);
      goBtn.type = "button";
      goBtn.textContent = "Go";

      L.DomEvent.disableClickPropagation(container);
      L.DomEvent.disableScrollPropagation(container);

      function goToCoordinates() {
        const coords = parseCoordinates(input.value);
        if (!coords) {
          error.textContent = "Could not read those coordinates. Example: 55.7558, 37.6173";
          error.hidden = false;
          return;
        }
        error.hidden = true;
        map.setView([coords.lat, coords.lon], Math.max(map.getZoom(), 14));
      }

      toggle.addEventListener("click", () => {
        panel.classList.toggle("open");
        if (panel.classList.contains("open")) input.focus();
      });
      goBtn.addEventListener("click", goToCoordinates);
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          goToCoordinates();
        }
      });

      return container;
    },
  });

  // Registered globally so the body tag's x-data="garminApp()" can reach it.
  // app.js is a plain (non-deferred) script and always runs before Alpine's
  // deferred one, so this is already defined by the time Alpine evaluates it.
  window.garminApp = function garminApp() {
    return {
      bbox: null,
      status: { kind: "idle", text: "Select a bbox or upload an OSM/PBF file" },
      previewStatus: { kind: "idle", text: "" },
      previewTitle: "Build a preview of the selected area",
      log: "",
      logVisible: false,
      downloadHref: "#",
      downloadVisible: false,
      queueText: "Queue: —",
      buildDisabled: true,
      cancelDisabled: true,
      clearDisabled: true,
      previewDisabled: true,
      basemaps: BASEMAPS.map(({ id, name }) => ({ id, name })),
      selectedBasemap: "osm",
      mapName: "",
      historyJobs: [],
      historyFilter: "",
      regionNames: [],
      currentJobId: null,

      get west() {
        return this.bbox ? fmt(this.bbox.west) : "—";
      },
      get south() {
        return this.bbox ? fmt(this.bbox.south) : "—";
      },
      get east() {
        return this.bbox ? fmt(this.bbox.east) : "—";
      },
      get north() {
        return this.bbox ? fmt(this.bbox.north) : "—";
      },

      get regionsHint() {
        if (!this.regionNames.length) return "";
        return `Downloaded and outlined on the map: ${this.regionNames.join(", ")}. A preview can only be built inside them.`;
      },

      get filteredHistoryJobs() {
        const query = (this.historyFilter || "").trim().toLowerCase();
        return this.historyJobs.filter((job) => {
          if (!query) return true;
          const hay = `${this.jobTitle(job)} ${this.bboxLine(job)}`.toLowerCase();
          return hay.includes(query);
        });
      },

      jobTitle(job) {
        return (job.name || "").trim() || "Untitled";
      },
      bboxLine(job) {
        return `${fmt(job.west)}…${fmt(job.east)}, ${fmt(job.south)}…${fmt(job.north)}`;
      },
      statusLabel(job) {
        return STATUS_LABEL[job.status] || job.status;
      },
      fmtDate,

      init() {
        initialView = readHash();

        map = L.map("map", { zoomControl: true }).setView(
          initialView ? [initialView.lat, initialView.lon] : [43.3, 42.5],
          initialView ? initialView.zoom : 9,
        );

        // OSM/style credits stay (they are required), only the "Leaflet" badge goes.
        map.attributionControl.setPrefix(false);

        L.control.scale({ metric: true, imperial: false, position: "bottomleft" }).addTo(map);

        const writeHash = () => {
          const center = map.getCenter();
          const hash = `#${map.getZoom()}/${center.lat.toFixed(5)}/${center.lng.toFixed(5)}`;
          history.replaceState(null, "", hash);
        };
        map.on("moveend", writeHash);
        writeHash();

        // Manual edits to the URL (paste, browser back/forward) jump the map too.
        window.addEventListener("hashchange", () => {
          const view = readHash();
          if (view) map.setView([view.lat, view.lon], view.zoom);
        });

        map.addControl(new GotoControl());

        // Above the basemap (tilePane, 200) and below everything drawn on it
        // (overlayPane, 400), and deaf to the mouse: a click over a region is
        // a click on the map, which is what the draw tool is waiting for.
        map.createPane(REGIONS_PANE);
        map.getPane(REGIONS_PANE).style.zIndex = 390;
        map.getPane(REGIONS_PANE).style.pointerEvents = "none";
        this.loadRegions();

        drawnItems = new L.FeatureGroup().addTo(map);
        drawControl = new L.Control.Draw({
          position: "topleft",
          draw: {
            polygon: false,
            polyline: false,
            circle: false,
            circlemarker: false,
            marker: false,
            rectangle: {
              shapeOptions: {
                color: "#2f5d3a",
                weight: 2,
                fillOpacity: 0.12,
              },
            },
          },
          edit: {
            featureGroup: drawnItems,
            remove: true,
          },
        });
        map.addControl(drawControl);

        map.on(L.Draw.Event.CREATED, (e) => {
          drawnItems.clearLayers();
          drawnLayer = e.layer;
          drawnItems.addLayer(drawnLayer);
          this.setBboxFromBounds(drawnLayer.getBounds());
        });

        map.on(L.Draw.Event.EDITED, (e) => {
          e.layers.eachLayer((layer) => {
            drawnLayer = layer;
            this.setBboxFromBounds(layer.getBounds());
          });
        });

        map.on(L.Draw.Event.DELETED, () => {
          this.clearBbox();
        });

        let savedBasemap = "osm";
        try {
          savedBasemap = localStorage.getItem(BASEMAP_STORAGE_KEY) || "osm";
        } catch {
          savedBasemap = "osm";
        }
        // A preview from a previous session is not on the map yet, and its file may
        // have been pruned since; fall back to the raster default until one is built.
        this.setBasemap(savedBasemap === PREVIEW_BASEMAP_ID ? "osm" : savedBasemap);

        this.loadStyleSpec();

        this.refreshQueue();
        this.refreshHistory();
        setInterval(() => {
          this.refreshQueue();
          this.refreshHistory();
        }, 3000);

        const saved = loadCurrentJob();
        if (saved?.bbox) {
          this.applyBbox(saved.bbox, { announce: false, fit: true });
        }
        if (saved?.jobId) {
          this.buildDisabled = true;
          this.setCancelEnabled(false);
          this.logVisible = true;
          this.setStatus("running", `Restoring job ${saved.jobId}…`);
          this.pollJob(saved.jobId);
        }
      },

      // The covered area, drawn once at startup: which regions the service has
      // downloaded only changes when the tile job brings a new one in.
      async loadRegions() {
        let data = null;
        try {
          const res = await fetch("/regions");
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          data = await res.json();
        } catch (err) {
          console.warn("could not load the region outlines", err);
          return;
        }
        const features = (data && data.features) || [];
        if (!features.length) return;
        regionsLayer = L.geoJSON(data, {
          pane: REGIONS_PANE,
          interactive: false,
          style: () => REGION_STYLE,
        }).addTo(map);
        this.regionNames = features
          .map((feature) => (feature.properties && feature.properties.name) || "")
          .filter(Boolean);
      },

      async loadStyleSpec() {
        try {
          styleSpec = await (await fetch("/vector/config")).json();
        } catch (err) {
          console.warn("could not ask the service about the map style", err);
        }
        if (!styleSpec || !styleSpec.available) {
          this.previewTitle = (styleSpec && styleSpec.reason) || "the map style is not installed";
          styleSpec = null;
          this.updatePreviewEnabled();
          return;
        }
        // A URL already pointing somewhere wins over the server's default center.
        if (!initialView && Array.isArray(styleSpec.center) && styleSpec.center.length === 2) {
          map.setView(styleSpec.center, styleSpec.zoom || map.getZoom());
        }
        this.updatePreviewEnabled();
      },

      async setBasemap(id) {
        let layer = null;
        let chosen = id;
        if (id === PREVIEW_BASEMAP_ID && styleSpec && previewSpec && previewSpec.tiles) {
          try {
            layer = await previewBaseLayer(styleSpec, previewSpec);
          } catch (err) {
            console.warn("the preview did not render", err);
            this.setPreviewStatus("error", "The preview was built but did not render — see the console");
          }
        }
        if (!layer) {
          const spec = BASEMAPS.find((item) => item.id === id) || BASEMAPS[0];
          chosen = spec.id;
          layer = L.tileLayer(spec.url, {
            maxZoom: spec.maxZoom,
            attribution: spec.attribution,
            subdomains: "abc",
          });
        }
        if (baseLayer) {
          map.removeLayer(baseLayer);
        }
        baseLayer = layer.addTo(map);
        if (baseLayer.bringToBack) {
          baseLayer.bringToBack(); // the GL layer is not a grid layer and has no such method
        }
        this.selectedBasemap = chosen;
        try {
          localStorage.setItem(BASEMAP_STORAGE_KEY, chosen);
        } catch {
          /* quota / private mode */
        }
      },

      onBasemapChange() {
        this.setBasemap(this.selectedBasemap);
      },

      showPreviewOption(preview) {
        previewSpec = preview;
        const name = (styleSpec && styleSpec.name) || "Area preview";
        if (this.basemaps.some((option) => option.id === PREVIEW_BASEMAP_ID)) {
          this.basemaps = this.basemaps.map((option) =>
            option.id === PREVIEW_BASEMAP_ID ? { ...option, name } : option,
          );
        } else {
          this.basemaps = [...this.basemaps, { id: PREVIEW_BASEMAP_ID, name }];
        }
        this.setBasemap(PREVIEW_BASEMAP_ID);
      },

      setStatus(kind, text) {
        this.status = { kind, text };
      },

      setPreviewStatus(kind, text) {
        this.previewStatus = { kind, text: text || "" };
      },

      hideDownload() {
        this.downloadVisible = false;
        this.downloadHref = "#";
      },

      updatePreviewEnabled() {
        // A preview needs only an area: no name, no uploaded file, and it does not
        // wait for the build queue - the worker behind it is a different one.
        this.previewDisabled =
          !styleSpec || !this.bbox || Boolean(previewTimer) || bboxMaxSideKm(this.bbox) > MAX_BBOX_SIDE_KM;
      },

      updateBuildEnabled() {
        this.updatePreviewEnabled();
        if (pollTimer) {
          this.buildDisabled = true;
          return;
        }
        const name = (this.mapName || "").trim();
        if (!name) {
          this.buildDisabled = true;
          return;
        }
        if (osmFile) {
          this.buildDisabled = osmFile.size > MAX_UPLOAD_BYTES;
          this.clearDisabled = false;
          return;
        }
        if (!this.bbox) {
          this.buildDisabled = true;
          return;
        }
        this.buildDisabled = bboxMaxSideKm(this.bbox) > MAX_BBOX_SIDE_KM;
      },

      setCancelEnabled(on) {
        this.cancelDisabled = !on;
      },

      stopPreviewPoll() {
        if (previewTimer) {
          clearInterval(previewTimer);
          previewTimer = null;
        }
      },

      stopPoll() {
        if (pollTimer) {
          clearInterval(pollTimer);
          pollTimer = null;
        }
      },

      applyBbox(next, { announce = true, fit = false } = {}) {
        this.bbox = {
          west: next.west,
          south: next.south,
          east: next.east,
          north: next.north,
        };
        this.clearDisabled = false;

        const sideKm = bboxMaxSideKm(this.bbox);
        const tooBig = sideKm > MAX_BBOX_SIDE_KM;
        this.updateBuildEnabled();

        drawnItems.clearLayers();
        drawnLayer = L.rectangle(
          [
            [this.bbox.south, this.bbox.west],
            [this.bbox.north, this.bbox.east],
          ],
          {
            color: "#2f5d3a",
            weight: 2,
            fillOpacity: 0.12,
          },
        );
        drawnItems.addLayer(drawnLayer);
        if (fit) {
          map.fitBounds(drawnLayer.getBounds(), { padding: [32, 32] });
        }
        if (announce) {
          this.hideDownload();
          if (tooBig) {
            this.setStatus(
              "error",
              `Every side of the bbox must be at most ${MAX_BBOX_SIDE_KM} km (this one is ${Math.round(sideKm)} km)`,
            );
          } else {
            this.setStatus("idle", "BBox selected. Press “Build map”.");
          }
        }
      },

      setBboxFromBounds(bounds) {
        this.applyBbox({
          west: bounds.getWest(),
          south: bounds.getSouth(),
          east: bounds.getEast(),
          north: bounds.getNorth(),
        });
      },

      clearBbox() {
        drawnItems.clearLayers();
        drawnLayer = null;
        this.bbox = null;
        this.buildDisabled = true;
        this.cancelDisabled = true;
        this.clearDisabled = true;
        osmFile = null;
        if (this.$refs.osmFile) {
          this.$refs.osmFile.value = "";
        }
        this.stopPoll();
        this.stopPreviewPoll();
        this.setPreviewStatus("idle", "");
        this.hideDownload();
        this.logVisible = false;
        this.log = "";
        this.setStatus("idle", "Select a bbox or upload an OSM/PBF file");
      },

      saveCurrentJob(jobId, jobBbox) {
        this.currentJobId = jobId;
        try {
          localStorage.setItem(JOB_STORAGE_KEY, JSON.stringify({ jobId, bbox: jobBbox }));
        } catch {
          /* quota / private mode */
        }
      },

      clearCurrentJob() {
        this.currentJobId = null;
        try {
          localStorage.removeItem(JOB_STORAGE_KEY);
        } catch {
          /* ignore */
        }
      },

      onFileChange(event) {
        const file = event.target.files && event.target.files[0];
        osmFile = file || null;
        if (osmFile && osmFile.size > MAX_UPLOAD_BYTES) {
          this.setStatus("error", "The file is larger than 200 MB");
          osmFile = null;
          event.target.value = "";
          this.updateBuildEnabled();
          return;
        }
        if (osmFile) {
          this.setStatus("idle", `File: ${osmFile.name} (${Math.round(osmFile.size / 1024 / 1024)} MB). Press “Build map”.`);
        }
        this.updateBuildEnabled();
      },

      async requestPreview() {
        if (!this.bbox || !styleSpec) return;
        this.previewDisabled = true;
        this.setPreviewStatus("running", "Requesting a preview…");
        try {
          const res = await fetch("/preview", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(this.bbox),
          });
          const data = await res.json();
          if (!res.ok) {
            throw new Error(data.error || `HTTP ${res.status}`);
          }
          if (!this.renderPreview(data)) {
            this.pollPreview(data.preview_id);
          }
        } catch (err) {
          this.setPreviewStatus("error", String(err.message || err));
          this.updatePreviewEnabled();
        }
      },

      // Returns true once the preview has reached a final state.
      renderPreview(data) {
        if (data.status === "done") {
          this.stopPreviewPoll();
          const mb = Math.max(1, Math.round((data.size_bytes || 0) / 1e6));
          const ttl = fmtPreviewTtl(data.expires_in_seconds);
          this.setPreviewStatus("done", `Ready (${mb} MB) — shown on the map${ttl}`);
          this.showPreviewOption(data);
          this.updatePreviewEnabled();
          return true;
        }
        if (data.status === "error") {
          this.stopPreviewPoll();
          this.setPreviewStatus("error", data.error || "The preview build failed");
          this.updatePreviewEnabled();
          return true;
        }
        // A preview sitting in "queued" for a minute means nothing is consuming the
        // queue - almost always the tilesvc-preview container is not running.
        const stalled =
          data.status === "queued" && (data.age_seconds || 0) > 60
            ? " — is the preview worker running?"
            : "";
        this.setPreviewStatus("running", `${data.message || data.status}${stalled}`);
        return false;
      },

      pollPreview(previewId) {
        this.stopPreviewPoll();
        previewTimer = setInterval(async () => {
          try {
            const res = await fetch(`/preview/${previewId}`);
            const data = await res.json();
            if (!res.ok) {
              throw new Error(data.error || `HTTP ${res.status}`);
            }
            this.renderPreview(data);
          } catch (err) {
            this.stopPreviewPoll();
            this.setPreviewStatus("error", String(err.message || err));
            this.updatePreviewEnabled();
          }
        }, 2000);
        this.updatePreviewEnabled();
      },

      renderQueue(queued, running) {
        const waiting = Number(queued) || 0;
        const busy = Number(running) || 0;
        this.queueText = busy ? `Queue: ${waiting} · building: ${busy}` : `Queue: ${waiting}`;
      },

      async refreshHistory() {
        try {
          const res = await fetch("/jobs");
          const data = await res.json();
          if (!res.ok) return;
          this.historyJobs = data.jobs || [];
          if (data.queued != null) {
            this.renderQueue(data.queued, data.running);
          }
        } catch {
          /* ignore */
        }
      },

      async refreshQueue() {
        try {
          const res = await fetch("/queue");
          const data = await res.json();
          if (!res.ok) return;
          this.renderQueue(data.queued, data.running);
        } catch {
          /* ignore polling errors */
        }
      },

      openHistoryJob(job) {
        this.applyBbox(job, { announce: false, fit: true });
        this.saveCurrentJob(job.job_id, {
          west: job.west,
          south: job.south,
          east: job.east,
          north: job.north,
        });
        this.hideDownload();
        this.logVisible = true;
        this.log = "";
        this.buildDisabled = true;
        this.mapName = job.name || "";
        this.pollJob(job.job_id);
      },

      async requestCancel(jobId) {
        if (!jobId) return;
        this.setCancelEnabled(false);
        this.setStatus("running", "Cancelling…");
        try {
          const res = await fetch(`/jobs/${jobId}/cancel`, { method: "POST" });
          const data = await res.json();
          if (res.status === 404) {
            throw new Error(data.error || "Job not found");
          }
          if (res.status === 403) {
            this.setStatus("error", data.error || "Only whoever started this build can cancel it");
            this.refreshHistory();
            return;
          }
          if (!res.ok && res.status !== 409) {
            throw new Error(data.error || `HTTP ${res.status}`);
          }
          this.renderQueue(data.queued, data.running);
          this.refreshHistory();
          if (jobId === this.currentJobId) {
            this.pollJob(jobId);
          }
        } catch (err) {
          this.setStatus("error", String(err.message || err));
          this.setCancelEnabled(true);
        }
      },

      onCancelClick() {
        this.requestCancel(this.currentJobId);
      },

      async onBuildClick() {
        const name = (this.mapName || "").trim();
        if (!name) {
          this.setStatus("error", "Give the map a name, so you can find it in the history later.");
          this.$refs.mapName.focus();
          return;
        }
        if (!osmFile && !this.bbox) return;
        if (!osmFile) {
          const sideKm = bboxMaxSideKm(this.bbox);
          if (sideKm > MAX_BBOX_SIDE_KM) {
            this.setStatus(
              "error",
              `Every side of the bbox must be at most ${MAX_BBOX_SIDE_KM} km (this one is ${Math.round(sideKm)} km)`,
            );
            return;
          }
        }
        this.buildDisabled = true;
        this.setCancelEnabled(false);
        this.hideDownload();
        this.setStatus("running", "Sending the request…");
        this.logVisible = true;
        this.log = "";

        try {
          let res;
          if (osmFile) {
            const body = new FormData();
            body.append("name", name);
            body.append("file", osmFile);
            res = await fetch("/maps/upload", { method: "POST", body });
          } else {
            res = await fetch("/maps", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ ...this.bbox, name }),
            });
          }
          const data = await res.json();
          if (!res.ok) {
            throw new Error(data.error || `HTTP ${res.status}`);
          }
          this.renderQueue(data.queued, data.running);
          this.saveCurrentJob(data.job_id, this.bbox);
          this.setCancelEnabled(Boolean(data.cancellable));
          this.refreshHistory();
          this.setStatus("running", `${name}\nStatus: ${data.status}\nQueue: ${data.queued ?? "—"}`);
          this.pollJob(data.job_id);
        } catch (err) {
          this.setStatus("error", String(err.message || err));
          this.updateBuildEnabled();
        }
      },

      async pollJob(jobId) {
        this.stopPoll();
        this.currentJobId = jobId;
        const tick = async () => {
          try {
            const res = await fetch(`/jobs/${jobId}`);
            const data = await res.json();
            if (res.status === 404) {
              this.clearCurrentJob();
              throw new Error(data.error || "Job not found");
            }
            if (!res.ok) {
              throw new Error(data.error || `HTTP ${res.status}`);
            }

            const lines = (data.log || []).slice(-20);
            this.logVisible = true;
            this.log = lines.join("\n");
            this.$nextTick(() => {
              if (this.$refs.log) this.$refs.log.scrollTop = this.$refs.log.scrollHeight;
            });

            if (data.name) {
              this.mapName = data.name;
            }
            const title = data.name || "Map";

            this.renderQueue(data.queued, data.running);

            if (data.status === "done") {
              this.stopPoll();
              this.setCancelEnabled(false);
              this.setStatus("done", `${title}\nReady: ${data.parts || 1} part(s)\n${data.message || ""}`);
              this.downloadHref = `/jobs/${jobId}/download`;
              this.downloadVisible = true;
              this.updateBuildEnabled();
              this.refreshHistory();
              return true;
            }
            if (data.status === "cancelled") {
              this.stopPoll();
              this.setCancelEnabled(false);
              this.setStatus("cancelled", `${title}\nCancelled`);
              this.updateBuildEnabled();
              this.refreshHistory();
              return true;
            }
            if (data.status === "error") {
              this.stopPoll();
              this.setCancelEnabled(false);
              this.setStatus("error", data.error || data.message || "The build failed");
              this.updateBuildEnabled();
              this.refreshHistory();
              return true;
            }
            this.setCancelEnabled(Boolean(data.cancellable));
            const queueLine =
              data.status === "queued"
                ? `\nQueue: ${data.queued ?? "—"}`
                : data.queued
                  ? `\nQueue: ${data.queued}`
                  : "";
            this.setStatus("running", `${title}\n${data.status}\n${data.message || ""}${queueLine}`);
            return false;
          } catch (err) {
            this.stopPoll();
            this.setCancelEnabled(false);
            this.setStatus("error", String(err.message || err));
            this.updateBuildEnabled();
            return true;
          }
        };
        const finished = await tick();
        if (!finished) {
          pollTimer = setInterval(tick, 3000);
        }
      },
    };
  };
})();
