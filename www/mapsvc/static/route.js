// Hiking routes on the map: click points, get a route from the routing
// service (www/routing, /route/api/v1 behind the same nginx), see it split by
// the kind of going, its elevation profile, and download it as GPX.
//
// window.otmRouteMixin(getMap) returns plain state and methods that app.js
// merges into the Alpine component; Leaflet objects stay in this closure, out
// of Alpine's reactive proxy, like the rest of app.js does.
(() => {
  // One colour per kind of going, the same on the map, in the profile strip
  // and in the legend. Checked for colour-blind separation against the panel;
  // off-trail is dashed on the map as well, so it never rests on hue alone.
  const GOING = {
    trail: { color: "#be123c", label: "trail", dash: null, weight: 4 },
    offroad: { color: "#ca8a04", label: "off trail", dash: "7 6", weight: 4 },
    ford: { color: "#2563eb", label: "ford", dash: null, weight: 6 },
  };
  const AREA_STYLE = { color: "#6d4c9f", weight: 1.5, dashArray: "2 5", fill: false };
  const INK_MUTED = "#5c6756";
  const LINE = "#c8c0b2";
  const PROFILE_FILL = "#2f5d3a";

  function haversine(a, b) {
    const R = 6371008.8;
    const rad = Math.PI / 180;
    const dLat = (b[1] - a[1]) * rad;
    const dLon = (b[0] - a[0]) * rad;
    const s = Math.sin(dLat / 2) ** 2 + Math.cos(a[1] * rad) * Math.cos(b[1] * rad) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(s));
  }

  function fmtDuration(seconds) {
    const minutes = Math.round(seconds / 60);
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    return h ? `${h} h ${String(m).padStart(2, "0")} min` : `${m} min`;
  }

  function fmtKm(m) {
    return m >= 10000 ? `${(m / 1000).toFixed(1)} km` : `${(m / 1000).toFixed(2)} km`;
  }

  function xmlEscape(text) {
    return String(text).replace(/[<>&"']/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&apos;" })[c]);
  }

  // GPX 1.1: the waypoints as <wpt>, the route as one track with elevation.
  function toGpx(route, name) {
    const whole = route.features[0];
    const props = whole.properties;
    const lines = [
      '<?xml version="1.0" encoding="UTF-8"?>',
      '<gpx version="1.1" creator="OpenTopoMap routing" xmlns="http://www.topografix.com/GPX/1/1">',
      `  <metadata><name>${xmlEscape(name)}</name><time>${new Date().toISOString()}</time></metadata>`,
    ];
    (props.waypoints || []).forEach((wp, i) => {
      const [lon, lat] = wp.snapped;
      lines.push(`  <wpt lat="${lat.toFixed(6)}" lon="${lon.toFixed(6)}"><name>${i + 1}</name></wpt>`);
    });
    const desc =
      `${fmtKm(props.distance_m)}, ${fmtDuration(props.duration_s)}, ` +
      `+${Math.round(props.ascent_m)} m / -${Math.round(props.descent_m)} m, ` +
      `off trail ${fmtKm(props.offroad_m)}` + (props.pack_kg ? `, pack ${props.pack_kg} kg` : "");
    lines.push(`  <trk><name>${xmlEscape(name)}</name><desc>${xmlEscape(desc)}</desc><trkseg>`);
    for (const [lon, lat, ele] of whole.geometry.coordinates) {
      lines.push(`    <trkpt lat="${lat.toFixed(6)}" lon="${lon.toFixed(6)}"><ele>${ele.toFixed(1)}</ele></trkpt>`);
    }
    lines.push("  </trkseg></trk>", "</gpx>", "");
    return lines.join("\n");
  }

  // Distance, elevation and kind of going at every vertex, from the stretches
  // (features 1..n), which together are the whole line in order.
  function profileSeries(route) {
    const d = [];
    const e = [];
    const g = [];
    const pts = [];
    let total = 0;
    let prev = null;
    for (const stretch of route.features.slice(1)) {
      for (const c of stretch.geometry.coordinates) {
        if (prev && prev[0] === c[0] && prev[1] === c[1]) continue;
        if (prev) total += haversine(prev, c);
        d.push(total);
        e.push(c[2]);
        g.push(stretch.properties.going);
        pts.push(c);
        prev = c;
      }
    }
    return { d, e, g, pts, total };
  }

  function niceStep(span, ticks) {
    const raw = span / Math.max(1, ticks);
    const mag = 10 ** Math.floor(Math.log10(raw));
    const steps = [1, 2, 2.5, 5, 10];
    return mag * steps.find((s) => s * mag >= raw);
  }

  function svgEl(tag, attrs, parent) {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    if (parent) parent.append(el);
    return el;
  }

  window.otmRouteMixin = function otmRouteMixin(getMap) {
    let markers = null; // L.LayerGroup of draggable waypoint markers
    let routeLayer = null;
    let areasLayer = null;
    let hoverMarker = null;
    let route = null; // the last answer, GeoJSON
    let series = null; // its profile
    let timer = null;
    // Aborting fetch only closes the browser connection: the routing worker
    // keeps running A*. Sending a request for every added point therefore
    // fills all four worker threads with abandoned searches on large graphs.
    // Keep one request in flight and coalesce every change into one reroute.
    let inflight = false;
    let pending = false;
    let requestSeq = 0;
    let api = "/route/api/v1";

    return {
      routeMode: false,
      routePoints: [],
      routeOffroad: true,
      routePack: 0,
      // pass limits (rtsa_scale): "" = any; at most routeTopPasses of that grade ("" = no limit)
      routeMaxGrade: "",
      routeTopPasses: "",
      routeUnrated: true,
      routeGrades: ["н/к", "1А", "1Б", "2А", "2Б", "3А", "3Б"],
      routePasses: [],
      routeStatus: { kind: "idle", text: "" },
      routeSummary: null,
      routeAreaNames: [],
      routeAreasLoaded: false,
      routeGoing: Object.entries(GOING).map(([id, s]) => ({ id, ...s })),

      async initRoute() {
        const map = getMap();
        markers = L.layerGroup().addTo(map);
        routeLayer = L.layerGroup().addTo(map);
        try {
          const cfg = await (await fetch("/routing/config")).json();
          if (cfg && cfg.api) api = cfg.api.replace(/\/$/, "");
        } catch {
          /* the same-origin default it is */
        }
        map.on("click", (e) => {
          if (this.routeMode) this.addRoutePoint(e.latlng);
        });
        // The rectangle tool wants the clicks while it draws.
        map.on(L.Draw.Event.DRAWSTART, () => this.setRouteMode(false));
        window.addEventListener("resize", () => this.drawProfile());
      },

      setRouteMode(on) {
        this.routeMode = on;
        const map = getMap();
        map.getContainer().classList.toggle("route-mode", on);
        if (on) {
          this.loadRouteAreas();
          if (!this.routePoints.length) {
            this.setRouteStatus("idle", "Click the map to add points; drag a point to move it, right-click to remove it.");
          }
        }
      },

      toggleRouteMode() {
        this.setRouteMode(!this.routeMode);
      },

      setRouteStatus(kind, text) {
        this.routeStatus = { kind, text };
      },

      async loadRouteAreas() {
        if (this.routeAreasLoaded) return;
        try {
          const res = await fetch(`${api}/areas`);
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const data = await res.json();
          areasLayer = L.geoJSON(data, { interactive: false, style: () => AREA_STYLE, pane: "otm-regions" }).addTo(getMap());
          this.routeAreaNames = data.features.filter((f) => f.properties.built).map((f) => f.properties.name);
          this.routeAreasLoaded = true;
        } catch (err) {
          this.setRouteStatus("error", `The routing service is not answering (${err.message || err})`);
        }
      },

      addRoutePoint(latlng) {
        const point = [latlng.lng, latlng.lat];
        this.routePoints = [...this.routePoints, point];
        this.redrawMarkers();
        this.scheduleRoute();
      },

      removeRoutePoint(index) {
        this.routePoints = this.routePoints.filter((_, i) => i !== index);
        this.redrawMarkers();
        this.scheduleRoute();
      },

      undoRoutePoint() {
        if (this.routePoints.length) this.removeRoutePoint(this.routePoints.length - 1);
      },

      clearRoute() {
        requestSeq += 1; // an answer already being computed is now stale
        pending = false;
        this.routePoints = [];
        this.redrawMarkers();
        this.showRoute(null);
        this.setRouteStatus("idle", this.routeMode ? "Click the map to add points." : "");
      },

      redrawMarkers() {
        markers.clearLayers();
        this.routePoints.forEach((p, i) => {
          const icon = L.divIcon({ className: "route-pin", html: `<span>${i + 1}</span>`, iconSize: [24, 24], iconAnchor: [12, 12] });
          const marker = L.marker([p[1], p[0]], { icon, draggable: true, keyboard: false, title: `Point ${i + 1}` });
          marker.on("dragend", () => {
            const ll = marker.getLatLng();
            this.routePoints = this.routePoints.map((q, j) => (j === i ? [ll.lng, ll.lat] : q));
            this.scheduleRoute(0);
          });
          marker.on("contextmenu", () => this.removeRoutePoint(i));
          marker.addTo(markers);
        });
      },

      onRouteOptionsChange() {
        this.scheduleRoute(0);
      },

      scheduleRoute(delay = 1000) {
        clearTimeout(timer);
        timer = setTimeout(() => this.buildRoute(), delay);
      },

      async buildRoute() {
        const seq = ++requestSeq;
        if (this.routePoints.length < 2) {
          this.showRoute(null);
          return;
        }
        if (inflight) {
          pending = true;
          return;
        }
        inflight = true;
        const pack = Math.max(0, Number(this.routePack) || 0);
        const body = {
          type: "FeatureCollection",
          properties: {
            allow_offroad: this.routeOffroad,
            pack_kg: pack,
            allow_unrated: this.routeUnrated,
            ...(this.routeMaxGrade ? { max_pass_grade: this.routeMaxGrade } : {}),
            ...(this.routeMaxGrade && this.routeTopPasses !== "" ? { max_top_passes: Number(this.routeTopPasses) } : {}),
          },
          features: this.routePoints.map((p) => ({ type: "Feature", geometry: { type: "Point", coordinates: p }, properties: {} })),
        };
        this.setRouteStatus("running", "Routing…");
        try {
          const res = await fetch(`${api}/route`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
          if (seq === requestSeq) {
            this.showRoute(data);
            this.setRouteStatus("done", "");
          }
        } catch (err) {
          if (seq === requestSeq) this.setRouteStatus("error", String(err.message || err));
        } finally {
          inflight = false;
          if (pending) {
            pending = false;
            this.buildRoute();
          }
        }
      },

      showRoute(data) {
        route = data;
        routeLayer.clearLayers();
        if (!data) {
          this.routeSummary = null;
          this.routePasses = [];
          series = null;
          this.drawProfile();
          this.$nextTick(() => getMap().invalidateSize());
          return;
        }
        L.geoJSON({ type: "FeatureCollection", features: data.features.slice(1) }, {
          style: (f) => {
            const s = GOING[f.properties.going] || GOING.trail;
            return { color: s.color, weight: s.weight, dashArray: s.dash, opacity: 0.9, lineCap: s.dash ? "butt" : "round" };
          },
          onEachFeature: (f, layer) => {
            const s = GOING[f.properties.going] || GOING.trail;
            const terrain = f.properties.terrain ? ` · ${f.properties.terrain}` : "";
            layer.bindTooltip(`${s.label}${terrain}: ${fmtKm(f.properties.distance_m)}, ${fmtDuration(f.properties.duration_s)}`, { sticky: true });
          },
        }).addTo(routeLayer);
        const p = data.features[0].properties;
        this.routeSummary = {
          distance: fmtKm(p.distance_m),
          duration: fmtDuration(p.duration_s),
          ascent: `${Math.round(p.ascent_m)} m`,
          descent: `${Math.round(p.descent_m)} m`,
          offroad: fmtKm(p.offroad_m),
          pack: p.pack_kg || 0,
        };
        this.routePasses = (p.passes || []).map((ps) => ({
          name: ps.name || "unnamed pass",
          grade: ps.grade || "no grade",
        }));
        series = profileSeries(data);
        // the profile strip appears under the map once Alpine has applied
        // x-show: then Leaflet re-measures and the plot knows its width
        this.$nextTick(() => {
          getMap().invalidateSize();
          this.drawProfile();
        });
      },

      downloadGpx() {
        if (!route) return;
        const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "");
        const name = `OpenTopoMap route ${stamp}`;
        const blob = new Blob([toGpx(route, name)], { type: "application/gpx+xml" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = `route-${stamp}.gpx`;
        document.body.append(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 1000);
      },

      // The elevation profile: one area series (elevation over distance), a
      // strip under it with the kind of going, a crosshair that follows the
      // pointer (or the arrow keys) and shows the spot on the map.
      drawProfile() {
        const host = this.$refs.profile;
        if (!host) return;
        host.replaceChildren();
        if (!series || series.d.length < 2) return;
        const width = host.clientWidth || 600;
        const height = host.clientHeight || 150; // set by the stylesheet
        const pad = { left: 44, right: 12, top: 10, bottom: 34 };
        const plotW = width - pad.left - pad.right;
        const plotH = height - pad.top - pad.bottom;
        const { d, e, g, pts, total } = series;
        let lo = Math.min(...e);
        let hi = Math.max(...e);
        const span = Math.max(hi - lo, 50);
        const step = niceStep(span, 3);
        lo = Math.floor((lo - span * 0.05) / step) * step;
        hi = Math.ceil((hi + span * 0.05) / step) * step;
        const x = (v) => pad.left + (v / total) * plotW;
        const y = (v) => pad.top + plotH - ((v - lo) / (hi - lo)) * plotH;

        const svg = svgEl("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img", tabindex: "0",
          "aria-label": `Elevation profile, ${fmtKm(total)}, from ${Math.round(Math.min(...e))} to ${Math.round(Math.max(...e))} m` }, host);

        // recessive grid and axis labels
        for (let v = lo; v <= hi + 1e-9; v += step) {
          svgEl("line", { x1: pad.left, x2: width - pad.right, y1: y(v), y2: y(v), stroke: LINE, "stroke-width": 1 }, svg);
          const t = svgEl("text", { x: pad.left - 6, y: y(v) + 4, "text-anchor": "end", class: "profile-axis" }, svg);
          t.textContent = `${Math.round(v)}`;
        }
        const kmStep = niceStep(total / 1000, Math.max(2, Math.floor(plotW / 90)));
        for (let k = 0; k <= total / 1000 + 1e-9; k += kmStep) {
          const t = svgEl("text", { x: x(k * 1000), y: height - 6, "text-anchor": k === 0 ? "start" : "middle", class: "profile-axis" }, svg);
          t.textContent = k === 0 ? "0 km" : `${+k.toFixed(1)}`;
        }

        // the area and its 2px top line
        let path = `M${x(d[0])},${y(e[0])}`;
        for (let i = 1; i < d.length; i++) path += `L${x(d[i]).toFixed(1)},${y(e[i]).toFixed(1)}`;
        svgEl("path", { d: `${path}L${x(total)},${pad.top + plotH}L${x(0)},${pad.top + plotH}Z`, fill: PROFILE_FILL, "fill-opacity": 0.14 }, svg);
        svgEl("path", { d: path, fill: "none", stroke: PROFILE_FILL, "stroke-width": 2, "stroke-linejoin": "round" }, svg);

        // the kind of going, as a strip under the plot
        const stripY = pad.top + plotH + 4;
        let start = 0;
        for (let i = 1; i <= g.length; i++) {
          if (i === g.length || g[i] !== g[start]) {
            const s = GOING[g[start]] || GOING.trail;
            const x0 = x(d[start]);
            const x1 = x(d[Math.min(i, d.length - 1)]);
            svgEl("rect", { x: x0, y: stripY, width: Math.max(1, x1 - x0 - 1), height: 6, rx: 1, fill: s.color }, svg);
            start = i;
          }
        }

        // hover: crosshair, dot, tooltip, and the spot on the map
        const cross = svgEl("line", { y1: pad.top, y2: pad.top + plotH, stroke: "#1c2418", "stroke-width": 1, visibility: "hidden" }, svg);
        const dot = svgEl("circle", { r: 4, fill: PROFILE_FILL, stroke: "#f7f4ee", "stroke-width": 2, visibility: "hidden" }, svg);
        const tip = document.createElement("div");
        tip.className = "profile-tip";
        tip.hidden = true;
        host.append(tip);
        const hit = svgEl("rect", { x: pad.left, y: 0, width: plotW, height, fill: "transparent" }, svg);
        const map = getMap();
        let current = -1;

        const show = (i) => {
          current = i;
          const px = x(d[i]);
          cross.setAttribute("x1", px);
          cross.setAttribute("x2", px);
          cross.setAttribute("visibility", "visible");
          dot.setAttribute("cx", px);
          dot.setAttribute("cy", y(e[i]));
          dot.setAttribute("visibility", "visible");
          const j = Math.min(i + 1, d.length - 1);
          const k = Math.max(i - 1, 0);
          const grade = d[j] > d[k] ? ((e[j] - e[k]) / (d[j] - d[k])) * 100 : 0;
          tip.replaceChildren();
          const strong = document.createElement("strong");
          strong.textContent = `${Math.round(e[i])} m`;
          const rest = document.createElement("span");
          rest.textContent = ` · ${fmtKm(d[i])} · ${grade >= 0 ? "+" : ""}${grade.toFixed(0)}% · ${(GOING[g[i]] || GOING.trail).label}`;
          tip.append(strong, rest);
          tip.hidden = false;
          tip.style.left = `${Math.min(px + 10, width - tip.offsetWidth - 4)}px`;
          tip.style.top = `${pad.top}px`;
          const ll = [pts[i][1], pts[i][0]];
          if (!hoverMarker) {
            hoverMarker = L.circleMarker(ll, { radius: 7, color: "#f7f4ee", weight: 2, fillColor: PROFILE_FILL, fillOpacity: 1, interactive: false });
          }
          hoverMarker.setLatLng(ll).addTo(map);
        };
        const hide = () => {
          current = -1;
          cross.setAttribute("visibility", "hidden");
          dot.setAttribute("visibility", "hidden");
          tip.hidden = true;
          if (hoverMarker) hoverMarker.remove();
        };
        const nearest = (px) => {
          const target = ((px - pad.left) / plotW) * total;
          let a = 0;
          let b = d.length - 1;
          while (b - a > 1) {
            const m = (a + b) >> 1;
            if (d[m] < target) a = m;
            else b = m;
          }
          return target - d[a] < d[b] - target ? a : b;
        };
        hit.addEventListener("pointermove", (ev) => {
          const box = svg.getBoundingClientRect();
          show(nearest(ev.clientX - box.left));
        });
        hit.addEventListener("pointerleave", hide);
        svg.addEventListener("blur", hide);
        svg.addEventListener("keydown", (ev) => {
          if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
          ev.preventDefault();
          const stepPx = ev.shiftKey ? plotW / 10 : plotW / 100;
          const from = current < 0 ? 0 : x(d[current]);
          show(nearest(Math.max(pad.left, Math.min(width - pad.right, from + (ev.key === "ArrowRight" ? stepPx : -stepPx)))));
        });
      },
    };
  };

  // exposed for tests and the console
  window.otmRoute = { toGpx, profileSeries, haversine };
})();
