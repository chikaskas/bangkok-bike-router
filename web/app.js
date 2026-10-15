"use strict";

const LTS_COLOR = { 1: "#16a34a", 2: "#84cc16", 3: "#f59e0b", 4: "#dc2626" };
const LTS_LABEL = { 1: "quiet", 2: "calm", 3: "busy", 4: "hostile" };
const MAX_LAMBDA = 12;

const $ = (id) => document.getElementById(id);
const fromEl = $("from"), toEl = $("to"), prefEl = $("pref");
const routesEl = $("routes"), statusEl = $("status"), prefNameEl = $("pref-name");
const summaryEl = $("summary"), legendEl = $("legend");
const optAltsEl = $("opt-alts"), optLegendEl = $("opt-legend");

/* `alts` is the comparison set from the default sweep; `current` is whatever is
   actually drawn, which for a slider drag is a fresh single-lambda plan. */
let alts = [];
let current = null;
let active = -1;
let lastQs = null;
let prefTimer = null;
let prefSeq = 0;
let pinLayer, routeLayer, activeLayer;

/* ---------------- map ---------------- */

const map = L.map("map", { zoomControl: false, preferCanvas: true })
  .setView([13.7466, 100.5347], 13);

L.control.zoom({ position: "bottomleft" }).addTo(map);

const streets = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
});
const cyclosm = L.tileLayer(
  "https://{s}.tile-cyclosm.openstreetmap.fr/cyclosm/{z}/{x}/{y}.png",
  {
    maxZoom: 19,
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &middot; ' +
      '<a href="https://www.cyclosm.org/">CyclOSM</a>',
  }
).addTo(map);
const plain = L.tileLayer(
  "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
  {
    maxZoom: 20,
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &middot; ' +
      '&copy; <a href="https://carto.com/attributions">CARTO</a>',
  }
);

L.control.layers(
  { "Bike map (CyclOSM)": cyclosm, "Standard streets": streets, "Plain": plain },
  {},
  { position: "topright" }
).addTo(map);

pinLayer = L.layerGroup().addTo(map);
routeLayer = L.layerGroup().addTo(map);
activeLayer = L.layerGroup().addTo(map);

/* ---------------- helpers ---------------- */

const ltsOf = (route, level) => (route.lts_km[String(level)] || 0);

/* Mirrors LAMBDA_SWEEP in src/config.py: the server plans one route per value
   and each survivor keeps its own, so naming them individually means two cards
   can never share a label. */
const LAMBDA_SWEEP = [0, 0.3, 0.8, 1.6, 3.0, 6.0, 12.0];
const TAG_NAMES = ["Fastest", "Quick", "Balanced", "Calm", "Relaxed", "Safe", "Safest"];

/* Boundaries are the log-space midpoints between consecutive sweep values, i.e.
   exactly where the nearest available route flips to the next one. */
const TAG_EDGES = LAMBDA_SWEEP.slice(1).map((hi, i) =>
  Math.expm1((Math.log1p(LAMBDA_SWEEP[i]) + Math.log1p(hi)) / 2)
);

const tagFor = (lam) => {
  const i = TAG_EDGES.findIndex((edge) => lam <= edge);
  return TAG_NAMES[i < 0 ? TAG_NAMES.length - 1 : i];
};

const labelForSlider = (p) => tagFor(sliderToLambda(p));

/* The handle is continuous: lambda is spaced logarithmically across the track
   so the low end (where route choice actually changes) gets most of the travel.
   Lambda values just inside each tag edge land where the old seven bands did. */
const MIN_LAMBDA = 0;
const LOG_SPAN = Math.log1p(MAX_LAMBDA);

const sliderToLambda = (p) => {
  const x = Math.max(0, Math.min(100, Number(p) || 0)) / 100;
  return Math.expm1(x * LOG_SPAN);
};

/* A tenth of a percent of the track is the finest the slider reports, which
   keeps a swept lambda's position to within ~0.2% of its true value. */
const lambdaToSlider = (lam) => {
  const y = Math.max(MIN_LAMBDA, Math.min(MAX_LAMBDA, Number(lam) || 0));
  return Math.round((Math.log1p(y) / LOG_SPAN) * 1000) / 10;
};

const fmtTime = (s) => {
  const m = Math.round(s / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
};

function pin(letter, latlon) {
  return L.marker(latlon, {
    icon: L.divIcon({
      className: "",
      html: `<div class="pin ${letter === "A" ? "a" : "b"}">${letter}</div>`,
      iconSize: [24, 24],
      iconAnchor: [12, 12],
    }),
    interactive: false,
  });
}

/** Split a route into runs of constant stress so the line can be coloured by exposure. */
function stressSegments(geom, lts) {
  const out = [];
  let s = 0;
  for (let i = 1; i < geom.length; i++) {
    if (lts[i] !== lts[s]) {
      out.push({ lts: lts[s], pts: geom.slice(s, i + 1) });
      s = i;
    }
  }
  if (s < geom.length - 1) out.push({ lts: lts[s], pts: geom.slice(s) });
  return out;
}

/* ---------------- autocomplete ---------------- */

function attachAutocomplete(input, box) {
  let items = [];
  let sel = -1;
  let timer = null;
  let req = 0;

  const close = () => {
    box.classList.remove("open");
    items = [];
    sel = -1;
  };

  const highlight = () => {
    [...box.children].forEach((el, i) => el.classList.toggle("sel", i === sel));
  };

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 3) return close();
    timer = setTimeout(async () => {
      const mine = ++req;
      try {
        const res = await fetch(`/api/search?q=${encodeURIComponent(q)}&limit=15`);
        const data = await res.json();
        if (mine !== req) return;
        items = data.results || [];
        sel = -1;
        box.innerHTML = items
          .map(
            (h) =>
              `<div class="ac-item"><span class="n">${escapeHtml(h.name)}</span>` +
              `<span class="k">${escapeHtml(h.kind || "")}</span></div>`
          )
          .join("");
        [...box.children].forEach((el, i) => {
          el.addEventListener("mousedown", (e) => {
            e.preventDefault();
            pick(i);
          });
        });
        box.classList.toggle("open", items.length > 0);
      } catch {
        close();
      }
    }, 170);
  });

  const pick = (i) => {
    const h = items[i];
    if (!h) return;
    input.value = h.name;
    input.dataset.lat = h.lat;
    input.dataset.lon = h.lon;
    close();
  };

  input.addEventListener("keydown", (e) => {
    if (!box.classList.contains("open")) {
      if (e.key === "Enter") find();
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      sel = Math.max(0, Math.min(items.length - 1, sel + (e.key === "ArrowDown" ? 1 : -1)));
      highlight();
    } else if (e.key === "Enter") {
      e.preventDefault();
      pick(sel < 0 ? 0 : sel);
      find();
    } else if (e.key === "Escape") {
      close();
    }
  });

  // A typed change invalidates any previously chosen coordinates.
  input.addEventListener("input", () => {
    delete input.dataset.lat;
    delete input.dataset.lon;
  });

  input.addEventListener("blur", () => setTimeout(close, 120));
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

/* ---------------- search + route ---------------- */

async function find() {
  const qs = new URLSearchParams();
  const push = (input, prefix) => {
    const v = input.value.trim();
    if (!v) throw new Error(`Enter a ${prefix === "from" ? "start" : "destination"}.`);
    if (input.dataset.lat && input.dataset.lon) {
      qs.set(`${prefix}_lat`, input.dataset.lat);
      qs.set(`${prefix}_lon`, input.dataset.lon);
    } else {
      qs.set(prefix, v);
    }
  };

  try {
    push(fromEl, "from");
    push(toEl, "to");
  } catch (err) {
    return setStatus(err.message, true);
  }

  setStatus("Finding routes…", false, true);
  $("go").disabled = true;

  try {
    const res = await fetch(`/api/route?${qs}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);

    lastQs = qs;
    alts = data.routes || [];
    drawPins(data);
    renderRoutes();

    if (!alts.length) {
      current = null;
      setStatus(data.note || "No route found between those points.", true);
      summaryEl.innerHTML = "";
      activeLayer.clearLayers();
      return;
    }
    setStatus(
      `${alts.length} route${alts.length > 1 ? "s" : ""} · ` +
        `${data.origin.label} → ${data.destination.label}`
    );
    choose(nearestToSlider(), { fit: true });
  } catch (err) {
    setStatus(err.message, true);
    alts = [];
    current = null;
    lastQs = null;
    routesEl.innerHTML = "";
    summaryEl.innerHTML = "";
    pinLayer.clearLayers();
    activeLayer.clearLayers();
    routeLayer.clearLayers();
  } finally {
    $("go").disabled = false;
  }
}

/** Re-plan for the slider's exact trade-off, so dragging changes the route smoothly. */
async function planAtSlider() {
  if (!lastQs) return;
  const lam = sliderToLambda(prefEl.value);
  const seq = ++prefSeq;

  setStatus("Updating route…", false, true);
  try {
    const qs = new URLSearchParams(lastQs);
    qs.set("lam", lam.toFixed(4));
    const res = await fetch(`/api/route?${qs}`);
    const data = await res.json();
    if (seq !== prefSeq) return; // a later drag already superseded this request
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);

    const r = (data.routes || [])[0];
    if (!r) throw new Error(data.note || "No route at that preference.");

    // Keep the comparison cards in step with what is drawn, even when the
    // slider lands between two of the swept routes.
    const idx = alts.findIndex((a) => a.key === r.key);
    if (idx !== active) {
      active = idx;
      markActive();
    }

    // Most slider positions resolve to the route already on screen; skipping the
    // redraw there keeps dragging from flickering.
    if (!current || current.key !== r.key) {
      current = r;
      drawRoutes();
    }
    renderSummary(r);
    setStatus(`${r.time_s ? fmtTime(r.time_s) : ""} · ${(r.distance_m / 1000).toFixed(1)} km · ${r.comfort_pct}% low-stress`);
  } catch (err) {
    if (seq === prefSeq) setStatus(err.message, true);
  }
}

function setStatus(msg, isError, isBusy) {
  statusEl.textContent = msg || "";
  statusEl.classList.toggle("error", !!isError);
  statusEl.classList.toggle("busy", !!isBusy);
}

/* ---------------- rendering ---------------- */

function drawPins(data) {
  pinLayer.clearLayers();
  const a = [data.origin.lat, data.origin.lon];
  const b = [data.destination.lat, data.destination.lon];
  pin("A", a).addTo(pinLayer);
  pin("B", b).addTo(pinLayer);
}

function stressBar(r) {
  const total = r.distance_m / 1000 || 1;
  return [1, 2, 3, 4]
    .filter((l) => ltsOf(r, l) > 0)
    .map((l) => {
      const km = ltsOf(r, l);
      return `<i style="flex:${km};background:${LTS_COLOR[l]}" title="LTS ${l} ${LTS_LABEL[l]} — ${km} km"></i>`;
    })
    .join("");
}

/** The one route on screen: a compact readout under the slider. */
function renderSummary(r) {
  if (!r) {
    summaryEl.innerHTML = "";
    return;
  }
  summaryEl.innerHTML = `
    <div class="summary-top">
      <span class="tag">${tagFor(r.lambda)}</span>
      <span class="time">${fmtTime(r.time_s)}</span>
      <span class="dist">${(r.distance_m / 1000).toFixed(1)} km</span>
    </div>
    <div class="bar">${stressBar(r)}</div>
    <div class="meta">
      <span>${r.comfort_pct}% low-stress</span>
      <span>avg ${r.avg_lts.toFixed(1)}/4</span>
    </div>`;
}

function markActive() {
  [...routesEl.children].forEach((el, j) => el.classList.toggle("active", j === active));
}

function renderRoutes() {
  routesEl.innerHTML = "";
  if (!alts.length) return;

  alts.forEach((r, i) => {
    const el = document.createElement("div");
    el.className = "card";
    el.innerHTML = `
      <div class="card-top">
        <span class="tag">${tagFor(r.lambda)}</span>
        <span class="time">${fmtTime(r.time_s)}</span>
      </div>
      <div class="meta">
        <span>${(r.distance_m / 1000).toFixed(1)} km</span>
        <span>${r.comfort_pct}% low-stress</span>
        <span>avg ${r.avg_lts.toFixed(1)}/4</span>
      </div>
      <div class="bar">${stressBar(r)}</div>
      <div class="reasons">
        ${(r.reasons || [])
          .slice(0, 3)
          .map((x) => `<span>${escapeHtml(x.reason)} ${x.km} km</span>`)
          .join("")}
      </div>`;
    el.addEventListener("click", () => choose(i));
    routesEl.appendChild(el);
  });

  markActive();
}

function drawRoutes(opts) {
  routeLayer.clearLayers();
  activeLayer.clearLayers();

  // Alternatives are off by default: one route on the map reads far better than
  // a tangle of grey ones. The toggle brings the comparison back.
  if (optAltsEl.checked) {
    const shown = current && current.key;
    alts.forEach((r, i) => {
      // Skip by identity, not just by index: a slider position between two
      // swept values leaves `active` at -1, and drawing the current route
      // again underneath itself just muddies the line.
      if (i === active || r.key === shown || !r.geometry) return;
      L.polyline(r.geometry, { color: "#64748b", weight: 3, opacity: 0.45 }).addTo(routeLayer);
    });
  }

  const r = current;
  if (!r || !r.geometry) return;

  const segs = r.geometry_lts ? stressSegments(r.geometry, r.geometry_lts) : [];
  if (segs.length) {
    segs.forEach((s) =>
      L.polyline(s.pts, {
        color: LTS_COLOR[s.lts] || "#64748b",
        weight: 6,
        opacity: 0.95,
        lineCap: "round",
      }).addTo(activeLayer)
    );
  } else {
    L.polyline(r.geometry, { color: "#38bdf8", weight: 6 }).addTo(activeLayer);
  }

  // Only re-frame on a new search; re-framing on every slider step is jarring.
  if (opts && opts.fit) {
    map.fitBounds(L.polyline(r.geometry).getBounds(), { padding: [40, 40] });
  }
}

/* ---------------- selection ---------------- */

function nearestToSlider() {
  if (!alts.length) return -1;
  const target = sliderToLambda(prefEl.value);
  let best = 0;
  let bestD = Infinity;
  alts.forEach((r, i) => {
    const d = Math.abs(Math.log1p(r.lambda) - Math.log1p(target));
    if (d < bestD) {
      bestD = d;
      best = i;
    }
  });
  return best;
}

/** Show an alternative from the comparison sweep and move the slider onto it. */
function choose(i, opts) {
  if (i < 0 || i >= alts.length) return;
  active = i;
  current = alts[i];
  prefEl.value = String(lambdaToSlider(current.lambda));
  prefNameEl.textContent = tagFor(current.lambda);
  drawRoutes(opts);
  renderSummary(current);
  markActive();
  routesEl.children[i]?.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

/* ---------------- wiring ---------------- */

attachAutocomplete(fromEl, $("from-ac"));
attachAutocomplete(toEl, $("to-ac"));

$("go").addEventListener("click", find);

prefEl.addEventListener("input", () => {
  prefNameEl.textContent = labelForSlider(prefEl.value);
  if (!lastQs) return;
  clearTimeout(prefTimer);
  prefTimer = setTimeout(planAtSlider, 160);
});

optAltsEl.addEventListener("change", () => {
  routesEl.hidden = !optAltsEl.checked;
  drawRoutes();
});

optLegendEl.addEventListener("change", () => {
  legendEl.hidden = !optLegendEl.checked;
});

// Prefill a demo trip so the app shows something useful on first load.
fromEl.value = "Khao San Road";
toEl.value = "Asok";
prefNameEl.textContent = labelForSlider(prefEl.value);
find();
