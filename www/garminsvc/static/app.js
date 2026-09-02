(() => {
  const el = {
    west: document.getElementById("v-west"),
    south: document.getElementById("v-south"),
    east: document.getElementById("v-east"),
    north: document.getElementById("v-north"),
    status: document.getElementById("status"),
    log: document.getElementById("log"),
    download: document.getElementById("download"),
    build: document.getElementById("btn-build"),
    cancel: document.getElementById("btn-cancel"),
    clear: document.getElementById("btn-clear"),
    queue: document.getElementById("queue-len"),
    history: document.getElementById("history"),
    historyEmpty: document.getElementById("history-empty"),
    basemap: document.getElementById("basemap"),
    preview: document.getElementById("btn-preview"),
    previewStatus: document.getElementById("preview-status"),
    mapName: document.getElementById("map-name"),
    historyFilter: document.getElementById("history-filter"),
    osmFile: document.getElementById("osm-file"),
  };

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
      attribution: `${OSM_ATTR}, стиль <a href="https://www.hotosm.org/">HOT</a> hosted by OSM France`,
    },
    {
      id: "otm",
      name: "OpenTopoMap",
      url: "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
      maxZoom: 17,
      attribution: `${OSM_ATTR}, <a href="https://viewfinderpanoramas.org">SRTM</a> | стиль: <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)`,
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
    queued: "в очереди",
    running: "сборка",
    done: "готово",
    error: "ошибка",
    cancelled: "отменено",
  };

  let bbox = null;
  let pollTimer = null;
  let previewTimer = null;
  let drawnLayer = null;
  let currentJobId = null;
  let historyJobs = [];

  // URL hash keeps the current view shareable/bookmarkable: #zoom/lat/lon.
  function readHash() {
    const match = /^#(\d{1,2}(?:\.\d+)?)\/(-?\d+(?:\.\d+)?)\/(-?\d+(?:\.\d+)?)$/.exec(location.hash);
    if (!match) return null;
    const [, zoomStr, latStr, lonStr] = match;
    const view = { zoom: Number(zoomStr), lat: Number(latStr), lon: Number(lonStr) };
    if (Math.abs(view.lat) > 90 || Math.abs(view.lon) > 180) return null;
    return view;
  }

  const initialView = readHash();

  const map = L.map("map", { zoomControl: true }).setView(
    initialView ? [initialView.lat, initialView.lon] : [43.3, 42.5],
    initialView ? initialView.zoom : 9,
  );
  let baseLayer = null;

  L.control.scale({ metric: true, imperial: false, position: "bottomleft" }).addTo(map);

  function writeHash() {
    const center = map.getCenter();
    const hash = `#${map.getZoom()}/${center.lat.toFixed(5)}/${center.lng.toFixed(5)}`;
    history.replaceState(null, "", hash);
  }
  map.on("moveend", writeHash);
  writeHash();

  // Manual edits to the URL (paste, browser back/forward) jump the map too.
  window.addEventListener("hashchange", () => {
    const view = readHash();
    if (view) map.setView([view.lat, view.lon], view.zoom);
  });

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

  const GotoControl = L.Control.extend({
    options: { position: "topright" },
    onAdd() {
      const container = L.DomUtil.create("div", "goto-control leaflet-bar");
      const toggle = L.DomUtil.create("button", "goto-toggle", container);
      toggle.type = "button";
      toggle.title = "Перейти к координатам";
      toggle.setAttribute("aria-label", "Перейти к координатам");
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
      hint.textContent = "Широта, долгота (или долгота, широта — формат определится сам)";
      const goBtn = L.DomUtil.create("button", "btn primary goto-go", panel);
      goBtn.type = "button";
      goBtn.textContent = "Перейти";

      L.DomEvent.disableClickPropagation(container);
      L.DomEvent.disableScrollPropagation(container);

      function goToCoordinates() {
        const coords = parseCoordinates(input.value);
        if (!coords) {
          error.textContent = "Не удалось распознать координаты. Пример: 55.7558, 37.6173";
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
  map.addControl(new GotoControl());

  // Public raster maps are what the picker shows; the service does not proxy
  // them. The one OTM-styled map it can produce is a preview of the drawn bbox,
  // which joins this list as an extra option once it has been built.
  let vectorLibs = null;
  let styleSpec = null; // /vector/config — MapLibre style assets and the DEM
  let previewSpec = null; // the built preview: {preview_id, tiles, …}

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = src;
      script.onload = () => resolve();
      script.onerror = () => reject(new Error(`не загрузился ${src}`));
      document.head.append(script);
    });
  }

  function ensureVectorLibs(spec) {
    if (!vectorLibs) {
      const css = document.createElement("link");
      css.rel = "stylesheet";
      css.href = MAPLIBRE_CSS;
      document.head.append(css);
      vectorLibs = (async () => {
        for (const src of [...VECTOR_LIBS, spec.layers, spec.style]) {
          await loadScript(src);
        }
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

  // MapLibre gets the style as an object, so it has no base URL to resolve our own
  // paths against; URL() is no help either because it would escape the {z}/{x}/{y}
  // braces of a tile template.
  function absoluteUrl(url) {
    return url.startsWith("/") ? window.location.origin + url : url;
  }

  async function previewBaseLayer(spec, preview) {
    await ensureVectorLibs(spec);
    return L.maplibreGL({
      attribution: spec.attribution,
      style: otmVectorStyle({
        // Zoom range and coverage come out of the file header, so MapLibre
        // asks for nothing outside the area that was built.
        url: `pmtiles://${preview.tiles}`,
        attribution: spec.attribution,
        dem: spec.dem ? { ...spec.dem, tiles: absoluteUrl(spec.dem.tiles) } : undefined,
        sprite: absoluteUrl(spec.sprite),
        globe: false, // Leaflet only knows Mercator
      }),
    });
  }

  async function setBasemap(id) {
    let layer = null;
    let chosen = id;
    if (id === PREVIEW_BASEMAP_ID && styleSpec && previewSpec && previewSpec.tiles) {
      try {
        layer = await previewBaseLayer(styleSpec, previewSpec);
      } catch (err) {
        console.warn("превью не отрисовалось", err);
        setPreviewStatus("error", "Превью собрано, но не отрисовалось — см. консоль");
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
    el.basemap.value = chosen;
    try {
      localStorage.setItem(BASEMAP_STORAGE_KEY, chosen);
    } catch {
      /* quota / private mode */
    }
  }

  for (const spec of BASEMAPS) {
    const option = document.createElement("option");
    option.value = spec.id;
    option.textContent = spec.name;
    el.basemap.append(option);
  }

  let savedBasemap = "osm";
  try {
    savedBasemap = localStorage.getItem(BASEMAP_STORAGE_KEY) || "osm";
  } catch {
    savedBasemap = "osm";
  }
  // A preview from a previous session is not on the map yet, and its file may
  // have been pruned since; fall back to the raster default until one is built.
  setBasemap(savedBasemap === PREVIEW_BASEMAP_ID ? "osm" : savedBasemap);
  el.basemap.addEventListener("change", () => setBasemap(el.basemap.value));

  (async () => {
    try {
      styleSpec = await (await fetch("/vector/config")).json();
    } catch (err) {
      console.warn("не удалось спросить про стиль карты", err);
    }
    if (!styleSpec || !styleSpec.available) {
      el.preview.title = (styleSpec && styleSpec.reason) || "стиль карты не установлен";
      styleSpec = null;
      updatePreviewEnabled();
      return;
    }
    // A URL already pointing somewhere wins over the server's default center.
    if (!initialView && Array.isArray(styleSpec.center) && styleSpec.center.length === 2) {
      map.setView(styleSpec.center, styleSpec.zoom || map.getZoom());
    }
    updatePreviewEnabled();
  })();

  const drawnItems = new L.FeatureGroup().addTo(map);
  const drawControl = new L.Control.Draw({
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

  const MAX_BBOX_SIDE_KM = 500;
  const MAX_UPLOAD_BYTES = 200 * 1024 * 1024;

  let osmFile = null;

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

  function bboxLine(job) {
    return `${fmt(job.west)}…${fmt(job.east)}, ${fmt(job.south)}…${fmt(job.north)}`;
  }

  function saveCurrentJob(jobId, jobBbox) {
    currentJobId = jobId;
    try {
      localStorage.setItem(
        JOB_STORAGE_KEY,
        JSON.stringify({ jobId, bbox: jobBbox }),
      );
    } catch {
      /* quota / private mode */
    }
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

  function clearCurrentJob() {
    currentJobId = null;
    try {
      localStorage.removeItem(JOB_STORAGE_KEY);
    } catch {
      /* ignore */
    }
  }

  function applyBbox(next, { announce = true, fit = false } = {}) {
    bbox = {
      west: next.west,
      south: next.south,
      east: next.east,
      north: next.north,
    };
    el.west.textContent = fmt(bbox.west);
    el.south.textContent = fmt(bbox.south);
    el.east.textContent = fmt(bbox.east);
    el.north.textContent = fmt(bbox.north);
    el.clear.disabled = false;

    const sideKm = bboxMaxSideKm(bbox);
    const tooBig = sideKm > MAX_BBOX_SIDE_KM;
    updateBuildEnabled();

    drawnItems.clearLayers();
    drawnLayer = L.rectangle(
      [
        [bbox.south, bbox.west],
        [bbox.north, bbox.east],
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
      hideDownload();
      if (tooBig) {
        setStatus(
          "error",
          `Каждая сторона bbox должна быть не больше ${MAX_BBOX_SIDE_KM} км (сейчас ${Math.round(sideKm)} км)`,
        );
      } else {
        setStatus("idle", "BBox выбран. Нажмите «Собрать карту».");
      }
    }
  }

  function setBboxFromBounds(bounds) {
    applyBbox({
      west: bounds.getWest(),
      south: bounds.getSouth(),
      east: bounds.getEast(),
      north: bounds.getNorth(),
    });
  }

  function clearBbox() {
    drawnItems.clearLayers();
    drawnLayer = null;
    bbox = null;
    for (const key of ["west", "south", "east", "north"]) {
      el[key].textContent = "—";
    }
    el.build.disabled = true;
    el.cancel.disabled = true;
    el.clear.disabled = true;
    osmFile = null;
    if (el.osmFile) {
      el.osmFile.value = "";
    }
    stopPoll();
    stopPreviewPoll();
    setPreviewStatus("idle", "");
    hideDownload();
    el.log.hidden = true;
    el.log.textContent = "";
    setStatus("idle", "Выберите bbox или загрузите OSM/PBF");
  }

  function setStatus(kind, text) {
    el.status.className = `status ${kind}`;
    el.status.textContent = text;
  }

  function hideDownload() {
    el.download.hidden = true;
    el.download.removeAttribute("href");
  }

  function updatePreviewEnabled() {
    // A preview needs only an area: no name, no uploaded file, and it does not
    // wait for the build queue - the worker behind it is a different one.
    el.preview.disabled =
      !styleSpec || !bbox || Boolean(previewTimer) || bboxMaxSideKm(bbox) > MAX_BBOX_SIDE_KM;
  }

  function setPreviewStatus(kind, text) {
    el.previewStatus.className = `preview-status ${kind}`;
    el.previewStatus.textContent = text || "";
    el.previewStatus.hidden = !text;
  }

  function stopPreviewPoll() {
    if (previewTimer) {
      clearInterval(previewTimer);
      previewTimer = null;
    }
  }

  function showPreviewOption(preview) {
    previewSpec = preview;
    let option = el.basemap.querySelector(`option[value="${PREVIEW_BASEMAP_ID}"]`);
    if (!option) {
      option = document.createElement("option");
      option.value = PREVIEW_BASEMAP_ID;
      el.basemap.append(option);
    }
    option.textContent = (styleSpec && styleSpec.name) || "Превью области";
    setBasemap(PREVIEW_BASEMAP_ID);
  }

  // Returns true once the preview has reached a final state.
  function renderPreview(data) {
    if (data.status === "done") {
      stopPreviewPoll();
      const mb = Math.max(1, Math.round((data.size_bytes || 0) / 1e6));
      setPreviewStatus("done", `Готово (${mb} МБ) — показано на карте`);
      showPreviewOption(data);
      updatePreviewEnabled();
      return true;
    }
    if (data.status === "error") {
      stopPreviewPoll();
      setPreviewStatus("error", data.error || "Ошибка сборки превью");
      updatePreviewEnabled();
      return true;
    }
    // A preview sitting in "queued" for a minute means nothing is consuming the
    // queue - almost always the tilesvc-preview container is not running.
    const stalled =
      data.status === "queued" && (data.age_seconds || 0) > 60
        ? " — воркер превью запущен?"
        : "";
    setPreviewStatus("running", `${data.message || data.status}${stalled}`);
    return false;
  }

  function pollPreview(previewId) {
    stopPreviewPoll();
    previewTimer = setInterval(async () => {
      try {
        const res = await fetch(`/preview/${previewId}`);
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.error || `HTTP ${res.status}`);
        }
        renderPreview(data);
      } catch (err) {
        stopPreviewPoll();
        setPreviewStatus("error", String(err.message || err));
        updatePreviewEnabled();
      }
    }, 2000);
    updatePreviewEnabled();
  }

  async function requestPreview() {
    if (!bbox || !styleSpec) return;
    el.preview.disabled = true;
    setPreviewStatus("running", "Запрашиваю превью…");
    try {
      const res = await fetch("/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(bbox),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      if (!renderPreview(data)) {
        pollPreview(data.preview_id);
      }
    } catch (err) {
      setPreviewStatus("error", String(err.message || err));
      updatePreviewEnabled();
    }
  }

  function updateBuildEnabled() {
    updatePreviewEnabled();
    if (pollTimer) {
      el.build.disabled = true;
      return;
    }
    const name = (el.mapName.value || "").trim();
    if (!name) {
      el.build.disabled = true;
      return;
    }
    if (osmFile) {
      el.build.disabled = osmFile.size > MAX_UPLOAD_BYTES;
      el.clear.disabled = false;
      return;
    }
    if (!bbox) {
      el.build.disabled = true;
      return;
    }
    el.build.disabled = bboxMaxSideKm(bbox) > MAX_BBOX_SIDE_KM;
  }

  function setCancelEnabled(on) {
    el.cancel.disabled = !on;
  }

  async function requestCancel(jobId) {
    if (!jobId) return;
    setCancelEnabled(false);
    setStatus("running", "Отмена…");
    try {
      const res = await fetch(`/jobs/${jobId}/cancel`, { method: "POST" });
      const data = await res.json();
      if (res.status === 404) {
        throw new Error(data.error || "Job not found");
      }
      if (res.status === 403) {
        setStatus("error", data.error || "Отменить сборку может только тот, кто её запустил");
        refreshHistory();
        return;
      }
      if (!res.ok && res.status !== 409) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      renderQueue(data.queued, data.running);
      refreshHistory();
      if (jobId === currentJobId) {
        pollJob(jobId);
      }
    } catch (err) {
      setStatus("error", String(err.message || err));
      setCancelEnabled(true);
    }
  }

  function renderQueue(queued, running) {
    const waiting = Number(queued) || 0;
    const busy = Number(running) || 0;
    if (busy) {
      el.queue.textContent = `Очередь: ${waiting} · в работе: ${busy}`;
    } else {
      el.queue.textContent = `Очередь: ${waiting}`;
    }
  }

  async function refreshHistory() {
    try {
      const res = await fetch("/jobs");
      const data = await res.json();
      if (!res.ok) return;
      historyJobs = data.jobs || [];
      renderHistory();
      if (data.queued != null) {
        renderQueue(data.queued, data.running);
      }
    } catch {
      /* ignore */
    }
  }

  function jobTitle(job) {
    return (job.name || "").trim() || "Без названия";
  }

  function renderHistory() {
    const query = (el.historyFilter.value || "").trim().toLowerCase();
    const jobs = historyJobs.filter((job) => {
      if (!query) return true;
      const hay = `${jobTitle(job)} ${bboxLine(job)}`.toLowerCase();
      return hay.includes(query);
    });
    el.history.replaceChildren();
    if (!historyJobs.length) {
      el.history.hidden = true;
      el.historyEmpty.hidden = false;
      el.historyEmpty.textContent = "Пока нет сохранённых сборок.";
      return;
    }
    if (!jobs.length) {
      el.history.hidden = true;
      el.historyEmpty.hidden = false;
      el.historyEmpty.textContent = "Ничего не найдено.";
      return;
    }
    el.history.hidden = false;
    el.historyEmpty.hidden = true;
    for (const job of jobs) {
      const item = document.createElement("li");
      const card = document.createElement("div");
      card.className = `history-item${job.job_id === currentJobId ? " active" : ""}`;
      card.addEventListener("click", () => openHistoryJob(job));

      const row = document.createElement("div");
      row.className = "row";
      const title = document.createElement("span");
      title.className = "title";
      title.textContent = jobTitle(job);
      const when = document.createElement("span");
      when.className = "when";
      when.textContent = fmtDate(job.created_at);
      row.append(title, when);

      const meta = document.createElement("div");
      meta.className = "row";
      const st = document.createElement("span");
      st.className = `st ${job.status}`;
      st.textContent = STATUS_LABEL[job.status] || job.status;
      meta.append(st);

      const coords = document.createElement("div");
      coords.className = "bbox-line";
      coords.textContent = bboxLine(job);

      card.append(row, meta, coords);
      if (job.downloadable) {
        const link = document.createElement("a");
        link.className = "dl";
        link.href = `/jobs/${job.job_id}/download`;
        link.textContent = "Скачать ZIP";
        link.addEventListener("click", (event) => event.stopPropagation());
        card.append(link);
      } else if (job.cancellable) {
        const stop = document.createElement("button");
        stop.type = "button";
        stop.className = "dl";
        stop.textContent = "Отменить";
        stop.addEventListener("click", (event) => {
          event.stopPropagation();
          requestCancel(job.job_id);
        });
        card.append(stop);
      }
      item.append(card);
      el.history.append(item);
    }
  }

  function openHistoryJob(job) {
    applyBbox(job, { announce: false, fit: true });
    saveCurrentJob(job.job_id, {
      west: job.west,
      south: job.south,
      east: job.east,
      north: job.north,
    });
    hideDownload();
    el.log.hidden = false;
    el.log.textContent = "";
    el.build.disabled = true;
    el.mapName.value = job.name || "";
    pollJob(job.job_id);
  }

  async function refreshQueue() {
    try {
      const res = await fetch("/queue");
      const data = await res.json();
      if (!res.ok) return;
      renderQueue(data.queued, data.running);
    } catch {
      /* ignore polling errors */
    }
  }

  function stopPoll() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  map.on(L.Draw.Event.CREATED, (e) => {
    drawnItems.clearLayers();
    drawnLayer = e.layer;
    drawnItems.addLayer(drawnLayer);
    setBboxFromBounds(drawnLayer.getBounds());
  });

  map.on(L.Draw.Event.EDITED, (e) => {
    e.layers.eachLayer((layer) => {
      drawnLayer = layer;
      setBboxFromBounds(layer.getBounds());
    });
  });

  map.on(L.Draw.Event.DELETED, () => {
    clearBbox();
  });

  el.mapName.addEventListener("input", updateBuildEnabled);

  el.osmFile.addEventListener("change", () => {
    const file = el.osmFile.files && el.osmFile.files[0];
    osmFile = file || null;
    if (osmFile && osmFile.size > MAX_UPLOAD_BYTES) {
      setStatus("error", "Файл больше 200 МБ");
      osmFile = null;
      el.osmFile.value = "";
      updateBuildEnabled();
      return;
    }
    if (osmFile) {
      setStatus("idle", `Файл: ${osmFile.name} (${Math.round(osmFile.size / 1024 / 1024)} МБ). Нажмите «Собрать карту».`);
    }
    updateBuildEnabled();
  });

  el.clear.addEventListener("click", clearBbox);

  el.preview.addEventListener("click", requestPreview);

  el.cancel.addEventListener("click", () => {
    requestCancel(currentJobId);
  });

  el.build.addEventListener("click", async () => {
    const name = (el.mapName.value || "").trim();
    if (!name) {
      setStatus("error", "Укажите название карты, чтобы потом найти её в истории.");
      el.mapName.focus();
      return;
    }
    if (!osmFile && !bbox) return;
    if (!osmFile) {
      const sideKm = bboxMaxSideKm(bbox);
      if (sideKm > MAX_BBOX_SIDE_KM) {
        setStatus(
          "error",
          `Каждая сторона bbox должна быть не больше ${MAX_BBOX_SIDE_KM} км (сейчас ${Math.round(sideKm)} км)`,
        );
        return;
      }
    }
    el.build.disabled = true;
    setCancelEnabled(false);
    hideDownload();
    setStatus("running", "Отправка запроса…");
    el.log.hidden = false;
    el.log.textContent = "";

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
          body: JSON.stringify({ ...bbox, name }),
        });
      }
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      renderQueue(data.queued, data.running);
      saveCurrentJob(data.job_id, bbox);
      setCancelEnabled(Boolean(data.cancellable));
      refreshHistory();
      setStatus("running", `${name}\nСтатус: ${data.status}\nОчередь: ${data.queued ?? "—"}`);
      pollJob(data.job_id);
    } catch (err) {
      setStatus("error", String(err.message || err));
      updateBuildEnabled();
    }
  });

  async function pollJob(jobId) {
    stopPoll();
    currentJobId = jobId;
    const tick = async () => {
      try {
        const res = await fetch(`/jobs/${jobId}`);
        const data = await res.json();
        if (res.status === 404) {
          clearCurrentJob();
          throw new Error(data.error || "Job not found");
        }
        if (!res.ok) {
          throw new Error(data.error || `HTTP ${res.status}`);
        }

        const lines = (data.log || []).slice(-20);
        el.log.hidden = false;
        el.log.textContent = lines.join("\n");
        el.log.scrollTop = el.log.scrollHeight;

        if (data.name) {
          el.mapName.value = data.name;
        }
        const title = data.name || "Карта";

        renderQueue(data.queued, data.running);

        if (data.status === "done") {
          stopPoll();
          setCancelEnabled(false);
          setStatus("done", `${title}\nГотово: ${data.parts || 1} часть(ей)\n${data.message || ""}`);
          el.download.href = `/jobs/${jobId}/download`;
          el.download.hidden = false;
          updateBuildEnabled();
          refreshHistory();
          return true;
        }
        if (data.status === "cancelled") {
          stopPoll();
          setCancelEnabled(false);
          setStatus("cancelled", `${title}\nОтменено`);
          updateBuildEnabled();
          refreshHistory();
          return true;
        }
        if (data.status === "error") {
          stopPoll();
          setCancelEnabled(false);
          setStatus("error", data.error || data.message || "Ошибка сборки");
          updateBuildEnabled();
          refreshHistory();
          return true;
        }
        setCancelEnabled(Boolean(data.cancellable));
        const queueLine =
          data.status === "queued"
            ? `\nОчередь: ${data.queued ?? "—"}`
            : data.queued
              ? `\nОчередь: ${data.queued}`
              : "";
        setStatus("running", `${title}\n${data.status}\n${data.message || ""}${queueLine}`);
        return false;
      } catch (err) {
        stopPoll();
        setCancelEnabled(false);
        setStatus("error", String(err.message || err));
        updateBuildEnabled();
        return true;
      }
    };
    const finished = await tick();
    if (!finished) {
      pollTimer = setInterval(tick, 3000);
    }
  }

  refreshQueue();
  refreshHistory();
  setInterval(() => {
    refreshQueue();
    refreshHistory();
  }, 3000);

  const saved = loadCurrentJob();
  if (saved?.bbox) {
    applyBbox(saved.bbox, { announce: false, fit: true });
  }
  if (saved?.jobId) {
    el.build.disabled = true;
    setCancelEnabled(false);
    el.log.hidden = false;
    setStatus("running", `Восстановление джоба ${saved.jobId}…`);
    pollJob(saved.jobId);
  }
})();
