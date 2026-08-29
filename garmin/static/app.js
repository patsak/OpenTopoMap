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
    mapName: document.getElementById("map-name"),
    historyFilter: document.getElementById("history-filter"),
    osmFile: document.getElementById("osm-file"),
  };

  const JOB_STORAGE_KEY = "otm-garmin-current-job";
  const BASEMAP_STORAGE_KEY = "otm-garmin-basemap";
  const OSM_ATTR =
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';
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
  let drawnLayer = null;
  let currentJobId = null;
  let historyJobs = [];

  const map = L.map("map", { zoomControl: true }).setView([43.3, 42.5], 9);
  let baseLayer = null;

  function setBasemap(id) {
    const spec = BASEMAPS.find((item) => item.id === id) || BASEMAPS[0];
    if (baseLayer) {
      map.removeLayer(baseLayer);
    }
    baseLayer = L.tileLayer(spec.url, {
      maxZoom: spec.maxZoom,
      attribution: spec.attribution,
      subdomains: "abc",
    }).addTo(map);
    baseLayer.bringToBack();
    el.basemap.value = spec.id;
    try {
      localStorage.setItem(BASEMAP_STORAGE_KEY, spec.id);
    } catch {
      /* ignore */
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
  setBasemap(savedBasemap);
  el.basemap.addEventListener("change", () => setBasemap(el.basemap.value));

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

  function updateBuildEnabled() {
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
