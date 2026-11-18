// Dashboard logic. Polls the JSON API and keeps the page up to date:
//   /api/cameras  every second  -> camera cards (added/removed as phones come and go), header
//   /api/events   every 3 s     -> alerts panel
//   /api/history  every 3 s     -> sparklines and charts
// The videos are plain <img> tags showing MJPEG streams; the boxes are drawn by the server,
// everything written on top of the video (LIVE, people, alerts) is HTML from this file.

const POLL_STATS_MS = 1000;
const POLL_SLOW_MS = 3000;
const SERIES_COUNT = 8;      // --series-1 ... --series-8 in style.css

const $ = (sel, root = document) => root.querySelector(sel);
const css = getComputedStyle(document.documentElement);
const color = (name) => css.getPropertyValue(name).trim();
const fmtTime = (ms, seconds = true) => new Date(ms).toLocaleTimeString([], {
  hour: "2-digit", minute: "2-digit", second: seconds ? "2-digit" : undefined, hour12: false,
});
const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (name, cls = "ic") => `<svg class="${cls}"><use href="#i-${name}"/></svg>`;
const videoUrl = (id) => `/video/${encodeURIComponent(id)}`;
const snapshotUrl = (id) => `/snapshot/${encodeURIComponent(id)}.jpg?t=${Date.now()}`;

// Over HTTP/1.1 (e.g. http://localhost:8000) a browser opens at most 6 connections to one
// server, and every MJPEG stream keeps one busy forever: with 6 cameras, nothing else
// (stats, alerts) could load. So there, only MAX_STREAMS tiles stream video and the others
// show still pictures refreshed several times a second. Through Caddy (HTTPS + HTTP/2) all
// requests share one connection, so every tile streams.
const HTTP2 = ["h2", "h3"].includes(performance.getEntriesByType("navigation")[0]?.nextHopProtocol);
const MAX_STREAMS = HTTP2 ? Infinity : 4;
const STILL_REFRESH_MS = 250;

// Set innerHTML only when it changed: no flicker, and hover states survive the 1 s refresh.
function setHtml(el, html) {
  if (el._html !== html) { el.innerHTML = html; el._html = html; }
}

// Event type -> label, severity, filter group, icon
const EVENT_TYPES = {
  motion: { label: "Motion", level: "warning", group: "motion", icon: "motion" },
  crowd: { label: "Crowd", level: "serious", group: "crowd", icon: "person" },
  camera_dark: { label: "Camera dark", level: "critical", group: "tamper", icon: "eye-off" },
  camera_blurry: { label: "Blurry", level: "critical", group: "tamper", icon: "eye-off" },
  camera_offline: { label: "Offline", level: "critical", group: "tamper", icon: "power" },
  camera_online: { label: "Back online", level: "good", group: "tamper", icon: "power" },
  phone_connected: { label: "Phone live", level: "good", group: "phone", icon: "phone" },
  phone_disconnected: { label: "Phone stopped", level: "none", group: "phone", icon: "phone" },
  system_start: { label: "System", level: "none", group: "system", icon: "power" },
};
const eventInfo = (type) => EVENT_TYPES[type] || { label: type, level: "warning", group: "other", icon: "alert" };

const state = {
  cameras: [], summary: null, serverTime: 0,
  history: {}, events: [], filter: "all", spotlight: null,
};

async function getJSON(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`${url}: ${res.status}`);
  return res.json();
}

// =============================================================== camera cards
const cards = new Map();  // cam_id -> { card, series }  (series = color 1..8, kept while the camera exists)

function freeSeries() {
  const used = new Set([...cards.values()].map((c) => c.series));
  for (let i = 1; i <= SERIES_COUNT; i++) if (!used.has(i)) return i;
  return (cards.size % SERIES_COUNT) + 1;
}
const seriesColor = (id) => color(`--series-${cards.get(id)?.series || 1}`);

function addCard(cam) {
  const card = $("#cam-template").content.firstElementChild.cloneNode(true);
  const series = freeSeries();
  card.dataset.id = cam.id;
  $(".swatch", card).style.background = `var(--series-${series})`;
  const img = $(".feed", card);
  img.alt = `Live video from ${cam.name}`;
  $(".video", card).addEventListener("click", () => openSpotlight(cam.id));
  $("#cams").appendChild(card);
  cards.set(cam.id, { card, series });
  if (!state.spotlight) startFeeds();
}

// Give each tile a video stream, or a refreshing still picture once MAX_STREAMS are in use.
function startFeeds() {
  let streams = 0;
  for (const [id, { card }] of cards) {
    const img = $(".feed", card);
    const mode = streams < MAX_STREAMS ? "video" : "still";
    if (mode === "video") streams++;
    if (img.dataset.mode === mode && img.getAttribute("src")) continue;
    img.dataset.mode = mode;
    img.src = mode === "video" ? videoUrl(id) : snapshotUrl(id);
  }
}

// Still pictures: load the next one only after the previous one arrived (no pile-up).
function refreshStills() {
  if (!state.spotlight) {
    for (const [id, { card }] of cards) {
      const img = $(".feed", card);
      const online = state.cameras.find((c) => c.id === id)?.status === "online";
      if (img.dataset.mode === "still" && img.complete && online) img.src = snapshotUrl(id);
    }
  }
  setTimeout(refreshStills, STILL_REFRESH_MS);
}

function removeCard(id) {  // (startFeeds() runs after removals, see syncCards)
  const { card } = cards.get(id);
  cards.delete(id);
  const img = $(".feed", card);
  img.removeAttribute("src");  // closes the video stream
  img.dataset.mode = "";
  card.classList.add("leaving");
  setTimeout(() => card.remove(), 300);
  if (state.spotlight === id) closeSpotlight();
}

function syncCards(cameras) {
  const ids = new Set(cameras.map((c) => c.id));
  const gone = [...cards.keys()].filter((id) => !ids.has(id));
  gone.forEach(removeCard);
  cameras.filter((c) => !cards.has(c.id)).forEach(addCard);
  if (gone.length && !state.spotlight) startFeeds();  // a freed stream slot goes to the next tile
}

const isTamper = (cam) => cam.alerts.includes("dark") || cam.alerts.includes("blurry");

// Message drawn over the video: tampering, no signal, waiting for phone. "" = none.
function veil(cam) {
  if (cam.status === "offline") {
    const age = cam.last_frame_age_s;
    return ["offline", `<strong>No signal</strong><span>${age ? `Last frame ${fmtAgo(age)} ago · ` : ""}retrying every 2 s</span>`];
  }
  if (cam.status === "waiting") {
    const age = cam.last_frame_age_s;
    const removeAfter = state.summary?.phone_remove_after_s ?? 60;
    const detail = age ? `Last frame ${fmtAgo(age)} ago · removed after ${removeAfter} s` : "Not streaming yet";
    return ["waiting", `${icon("phone")}<strong>Waiting for phone</strong><span>${detail}</span>`];
  }
  if (cam.alerts.includes("dark")) {
    return ["tamper", `${icon("eye-off")}<strong>Camera covered?</strong><span>Brightness ${cam.brightness} · for ${cam.alert_age_s.dark ?? 0} s</span>`];
  }
  if (cam.alerts.includes("blurry")) {
    return ["tamper", `${icon("eye-off")}<strong>Lens dirty?</strong><span>Sharpness ${cam.sharpness} · for ${cam.alert_age_s.blurry ?? 0} s</span>`];
  }
  return ["", ""];
}

function fmtAgo(s) {
  if (s < 90) return `${Math.round(s)} s`;
  return `${Math.round(s / 60)} min`;
}

// Connection quality of a phone, from its FPS and the upload round trip it reports.
function connection(cam) {
  const rtt = cam.rtt_ms, fps = cam.fps;
  if (fps >= 10 && (rtt == null || rtt < 300)) return ["good", rtt != null ? `Good · ${rtt} ms` : "Good"];
  if (fps >= 5 && (rtt == null || rtt < 1000)) return ["slow", `Slow · ${fps} fps`];
  return ["poor", `Poor · ${fps} fps`];
}

function renderCard(cam) {
  const { card } = cards.get(cam.id);
  const online = cam.status === "online";
  const tamper = online && isTamper(cam);
  const crowd = online && cam.alerts.includes("crowd");
  card.classList.toggle("crowd", crowd && !tamper);
  card.classList.toggle("tamper", tamper);
  card.classList.toggle("offline", cam.status === "offline");
  card.classList.toggle("waiting", cam.status === "waiting");
  $(".video", card).setAttribute("aria-label", `Open ${cam.name} large`);

  $(".name", card).textContent = cam.name;
  setHtml($(".kind", card), cam.kind === "phone"
    ? `${icon("phone", "ic sm")}${escapeHtml(cam.device || "Phone")}` : escapeHtml(cam.id));
  const status = $(".status", card);
  status.className = `status ${cam.status}`;
  $(".status-text", status).textContent = { online: "Online", offline: "Offline", waiting: "Waiting" }[cam.status];

  const people = $(".people", card);
  people.classList.toggle("serious", crowd);
  $(".people-text", card).textContent = `${cam.people} / ${cam.crowd_limit}`;

  const chips = [];
  if (crowd) chips.push(`<span class="chip serious">${icon("alert", "ic sm")}Crowd · limit ${cam.crowd_limit}</span>`);
  if (online && cam.alerts.includes("motion")) chips.push(`<span class="chip warning">${icon("motion", "ic sm")}Motion</span>`);
  if (online && cam.kind === "phone") {
    const [q, label] = connection(cam);
    chips.push(`<span class="chip"><span class="bars ${q}"><i></i><i></i><i></i></span>${label}</span>`);
  }
  setHtml($(".ov.bottom", card), chips.join(""));

  const [veilClass, veilHtml] = veil(cam);
  const v = $(".veil", card);
  v.className = `veil ${veilClass}${veilClass ? "" : " hidden"}`;
  setHtml(v, veilHtml);

  $(".numbers-line", card).textContent = online ? `motion ${cam.motion_pct.toFixed(1)}% · ${cam.fps} fps` : "";
  let health;
  if (cam.status === "offline") health = '<span class="tag bad">No signal</span>';
  else if (cam.status === "waiting") health = '<span class="tag">Not streaming · no alarm</span>';
  else if (cam.alerts.includes("dark")) health = '<span class="tag bad">Too dark</span>';
  else if (cam.alerts.includes("blurry")) health = '<span class="tag bad">Blurry</span>';
  else health = '<span class="tag">Image OK</span>';
  setHtml($(".health", card), health);
}

// =============================================================== header
function renderHeader(data) {
  const s = data.summary;
  $("#sum-online").textContent = s.online;
  $("#sum-total").textContent = ` / ${s.total} online`;
  $("#sum-people").textContent = s.people;
  $("#clock").textContent = fmtTime(data.server_time * 1000);
  const pill = $("#status-pill");
  if (s.active_alerts > 0) {
    pill.className = "status-pill alert";
    setHtml(pill, `${icon("alert", "ic sm")}${s.active_alerts} active alert${s.active_alerts > 1 ? "s" : ""}`);
  } else {
    pill.className = "status-pill ok";
    setHtml(pill, `${icon("check", "ic sm")}All clear`);
  }
}

async function pollCameras() {
  try {
    const data = await getJSON("/api/cameras");
    state.cameras = data.cameras;
    state.summary = data.summary;
    state.serverTime = data.server_time;
    syncCards(data.cameras);
    data.cameras.forEach(renderCard);
    renderHeader(data);
    if (state.spotlight) renderSpotlight();
    checkNewPhone();
    $("#conn-banner").classList.add("hidden");
  } catch (e) {
    $("#conn-banner").classList.remove("hidden");
  }
}

// =============================================================== charts (plain SVG)
function sparkSvg(rows, stroke, limit) {
  if (!rows.length) return "";
  const W = 120, H = 28;
  const vals = rows.map((r) => r.people);
  const peak = Math.max(...vals);
  const max = Math.max(limit || 1, peak, 1);
  const x = (i) => (vals.length === 1 ? W : (i / (vals.length - 1)) * W);
  const y = (v) => H - 2 - (v / max) * (H - 6);
  const pts = vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const limitLine = limit && peak >= limit * 0.75
    ? `<line x1="0" x2="${W}" y1="${y(limit)}" y2="${y(limit)}" stroke="${color("--serious")}" stroke-width="1" stroke-dasharray="3 3" vector-effect="non-scaling-stroke"/>` : "";
  return `${limitLine}<polyline points="${pts}" fill="none" stroke="${stroke}" stroke-width="2" vector-effect="non-scaling-stroke"/>`;
}

// Area chart with a few gridlines, start/end time labels and an optional limit line.
function areaChart(points, stroke, { limit = null, W = 320, H = 110 } = {}) {
  if (points.length < 2) return `<svg viewBox="0 0 ${W} ${H}"><text x="0" y="${H / 2}">Collecting data…</text></svg>`;
  const top = 12, bottom = H - 18;
  const t0 = points[0].t, t1 = points[points.length - 1].t;
  const max = Math.max(1, limit || 0, ...points.map((p) => p.v)) * 1.1;
  const x = (t) => ((t - t0) / Math.max(1, t1 - t0)) * W;
  const y = (v) => bottom - (v / max) * (bottom - top);
  const line = points.map((p) => `${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const grid = [0.5, 1].map((f) => `<line x1="0" x2="${W}" y1="${y(max * f / 1.1)}" y2="${y(max * f / 1.1)}" stroke="${color("--border")}"/>`).join("");
  const limitSvg = limit ? `<line x1="0" x2="${W}" y1="${y(limit)}" y2="${y(limit)}" stroke="${color("--serious")}" stroke-dasharray="4 4"/>
    <text x="${W}" y="${y(limit) - 5}" text-anchor="end" style="fill:${color("--serious-text")}">limit ${limit}</text>` : "";
  const last = points[points.length - 1];
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Chart, now ${last.v}">
    ${grid}<line x1="0" x2="${W}" y1="${bottom}" y2="${bottom}" stroke="${color("--border")}"/>
    <path d="M${line.replace(/ /g, " L")} L${W},${bottom} L0,${bottom} Z" fill="${stroke}" fill-opacity="0.18"/>
    <polyline points="${line}" fill="none" stroke="${stroke}" stroke-width="2"/>
    <circle cx="${x(last.t)}" cy="${y(last.v)}" r="3.5" fill="${stroke}"/>
    ${limitSvg}
    <text x="0" y="${H - 2}">${fmtTime(t0 * 1000, false)}</text>
    <text x="${W}" y="${H - 2}" text-anchor="end">now</text>
  </svg>`;
}

// Total people across all cameras, in 5-second steps (each camera's last value carried forward).
function totalPeople(history) {
  const now = Math.max(0, ...Object.values(history).flat().map((r) => r.t));
  if (!now) return [];
  const step = 5, start = now - 300;
  const out = [];
  for (let t = start; t <= now; t += step) {
    let sum = 0, any = false;
    for (const rows of Object.values(history)) {
      let v = null;
      for (const r of rows) { if (r.t <= t) v = r.people; else break; }
      if (v !== null) { sum += v; any = true; }
    }
    if (any) out.push({ t, v: sum });
  }
  return out;
}

async function pollHistory() {
  try {
    state.history = await getJSON("/api/history");
    for (const [id, { card }] of cards) {
      const cam = state.cameras.find((c) => c.id === id);
      setHtml($(".spark", card), sparkSvg(state.history[id] || [], seriesColor(id), cam?.crowd_limit));
    }
    const total = totalPeople(state.history);
    setHtml($("#people-chart"), areaChart(total, color("--accent")));
    $("#people-now").textContent = state.summary ? state.summary.people : "–";
    const delta = $("#people-delta");
    if (total.length > 1) {
      const d = total[total.length - 1].v - total[0].v;
      delta.textContent = d === 0 ? "no change in 5 min" : `${d > 0 ? "+" : "−"}${Math.abs(d)} since ${fmtTime(total[0].t * 1000, false)}`;
      delta.className = `delta${d > 0 ? " up" : ""}`;
    }
    if (state.spotlight) renderSpotlightChart();
  } catch (e) { /* retried on the next poll */ }
}

// =============================================================== alerts panel
let seenEvents = null;  // ids already shown (null = first load: nothing flashes)

function renderEvents() {
  const list = state.events.filter((e) => e.type !== "system_start"
    && (state.filter === "all" || eventInfo(e.type).group === state.filter));
  if (!list.length) {
    setHtml($("#events"), `<li class="empty">No ${state.filter === "all" ? "" : "matching "}alerts yet</li>`);
    return;
  }
  setHtml($("#events"), list.map((e) => {
    const info = eventInfo(e.type);
    const isNew = seenEvents && !seenEvents.has(e.id);
    const thumb = e.has_snapshot
      ? `<img src="/api/events/${e.id}/snapshot.jpg" alt="" loading="lazy">` : icon(info.icon);
    return `<li><button type="button" class="event${isNew ? " new" : ""}" data-cam="${escapeHtml(e.camera_id)}">
      <span class="thumb">${thumb}</span>
      <span style="display:flex;flex-direction:column;gap:2px;min-width:0">
        <span class="line1"><span class="badge ${info.level}">${info.label}</span><span class="who">${escapeHtml(e.camera_name)}</span><span class="when">${fmtTime(e.ts * 1000)}</span></span>
        <span class="msg">${escapeHtml(e.message)}</span>
      </span></button></li>`;
  }).join(""));
  seenEvents = new Set(state.events.map((e) => e.id));
}

async function pollEvents() {
  try {
    const events = await getJSON("/api/events?limit=60");
    const changed = events[0]?.id !== state.events[0]?.id;
    state.events = events;
    if (changed) renderEvents();
    if (state.spotlight) renderSpotlightEvents();
  } catch (e) { /* the connection banner is handled by pollCameras */ }
}

$("#events").addEventListener("click", (ev) => {
  const btn = ev.target.closest(".event");
  if (btn && cards.has(btn.dataset.cam)) openSpotlight(btn.dataset.cam);
});
document.querySelectorAll(".filter").forEach((btn) => btn.addEventListener("click", () => {
  document.querySelectorAll(".filter").forEach((b) => b.classList.toggle("on", b === btn));
  state.filter = btn.dataset.filter;
  renderEvents();
}));

// =============================================================== spotlight (one camera large)
function pauseGrid(paused) {
  if (!paused) { startFeeds(); return; }
  for (const { card } of cards.values()) {  // no video streams while the grid is hidden
    const img = $(".feed", card);
    img.removeAttribute("src");
    img.dataset.mode = "";
  }
}

function openSpotlight(id) {
  const switching = state.spotlight !== null;
  state.spotlight = id;
  if (!switching) pauseGrid(true);
  $("#overview").classList.add("hidden");
  $("#spotlight").classList.remove("hidden");
  $("#spot-img").src = videoUrl(id);
  $("#spot-save").href = `/snapshot/${encodeURIComponent(id)}.jpg?download=1`;
  renderSpotlight();
  renderSpotlightChart();
  renderSpotlightEvents();
  renderStrip();
  window.scrollTo(0, 0);
}

function closeSpotlight() {
  if (!state.spotlight) return;
  state.spotlight = null;
  $("#spot-img").removeAttribute("src");
  $("#spotlight").classList.add("hidden");
  $("#overview").classList.remove("hidden");
  pauseGrid(false);
}

function renderSpotlight() {
  const cam = state.cameras.find((c) => c.id === state.spotlight);
  if (!cam) { closeSpotlight(); return; }
  const online = cam.status === "online";
  const tamper = online && isTamper(cam);
  const crowd = online && cam.alerts.includes("crowd");

  $("#spot-swatch").style.background = seriesColor(cam.id);
  $("#spot-name").textContent = cam.name;
  $("#spot-source").textContent = cam.kind === "phone" ? `${cam.device || "Phone"} · ${cam.id}` : cam.id;
  $("#spot-img").alt = `Live video from ${cam.name}`;
  $("#spot-clock").textContent = fmtTime(state.serverTime * 1000);

  const pill = $("#spot-pill");
  let pillClass = "ok", pillHtml = `${icon("check", "ic sm")}No alerts`;
  if (cam.status === "offline") [pillClass, pillHtml] = ["alert", "No signal"];
  else if (cam.status === "waiting") [pillClass, pillHtml] = ["", "Waiting for phone"];
  else if (cam.alerts.includes("dark")) [pillClass, pillHtml] = ["alert", `${icon("alert", "ic sm")}Camera covered?`];
  else if (cam.alerts.includes("blurry")) [pillClass, pillHtml] = ["alert", `${icon("alert", "ic sm")}Lens dirty?`];
  else if (crowd) [pillClass, pillHtml] = ["crowd", `${icon("alert", "ic sm")}Crowd alert · ${cam.people} people, limit ${cam.crowd_limit}`];
  pill.className = `status-pill ${pillClass}`;
  setHtml(pill, pillHtml);

  const box = $("#spot-video");
  box.classList.toggle("crowd", crowd && !tamper);
  box.classList.toggle("tamper", tamper);
  const [veilClass, veilHtml] = veil(cam);
  const v = $("#spot-veil");
  v.className = `veil ${veilClass}${veilClass ? "" : " hidden"}`;
  setHtml(v, veilHtml);

  const num = (val, unit) => (online ? `${val}<small>${unit}</small>` : "–");
  setHtml($("#spot-people"), num(cam.people, ` / ${cam.crowd_limit}`));
  $("#spot-people").classList.toggle("serious", crowd);
  setHtml($("#spot-motion"), num(cam.motion_pct.toFixed(1), "%"));
  setHtml($("#spot-fps"), num(cam.fps, " fps"));
  setHtml($("#spot-yolo"), num(Math.round(cam.timing_ms.people), " ms"));

  // Brightness 0..255 on a straight scale; sharpness 0..~2000 on a log scale (values vary a lot).
  const pct = (f) => `${Math.max(0, Math.min(1, f)) * 100}%`;
  const sharpPos = (s) => Math.log10(s + 1) / Math.log10(2001);
  const dark = cam.brightness < cam.dark_threshold;
  const blurry = cam.sharpness < cam.blur_threshold && !dark;
  $("#spot-bright-val").textContent = online ? `${cam.brightness} · ${dark ? "too dark" : "OK"}` : "–";
  $("#spot-bright-fill").style.width = online ? pct(cam.brightness / 255) : "0";
  $("#spot-bright-fill").classList.toggle("bad", dark);
  $("#spot-bright-mark").style.left = pct(cam.dark_threshold / 255);
  $("#spot-sharp-val").textContent = online ? `${cam.sharpness} · ${blurry ? "blurry" : "OK"}` : "–";
  $("#spot-sharp-fill").style.width = online ? pct(sharpPos(cam.sharpness)) : "0";
  $("#spot-sharp-fill").classList.toggle("bad", blurry);
  $("#spot-sharp-mark").style.left = pct(sharpPos(cam.blur_threshold));
}

function renderSpotlightChart() {
  const cam = state.cameras.find((c) => c.id === state.spotlight);
  if (!cam) return;
  const rows = (state.history[cam.id] || []).map((r) => ({ t: r.t, v: r.people }));
  setHtml($("#spot-chart"), areaChart(rows, seriesColor(cam.id), { limit: cam.crowd_limit, W: 300, H: 100 }));
  renderStrip();
}

function renderSpotlightEvents() {
  const mine = state.events.filter((e) => e.camera_id === state.spotlight).slice(0, 6);
  setHtml($("#spot-events"), mine.length ? mine.map((e) => {
    const info = eventInfo(e.type);
    return `<li><span><span class="badge ${info.level}">${info.label}</span> ${escapeHtml(e.message)}</span><span class="when">${fmtTime(e.ts * 1000)}</span></li>`;
  }).join("") : '<li class="muted">No alerts for this camera yet</li>');
}

// Small previews of the other cameras (still pictures refreshed every 3 s, not video streams).
function renderStrip() {
  const strip = $("#spot-strip");
  const stamp = Math.floor(Date.now() / POLL_SLOW_MS);
  strip.innerHTML = state.cameras.map((c) => {
    const bad = c.status === "offline" || (c.status === "online" && isTamper(c));
    const img = c.status === "online" ? `<img src="/snapshot/${encodeURIComponent(c.id)}.jpg?t=${stamp}" alt="">` : "";
    return `<button type="button" data-id="${escapeHtml(c.id)}" class="${c.id === state.spotlight ? "current" : ""} ${bad ? "bad" : ""}" aria-label="Show ${escapeHtml(c.name)}">${img}<span>${escapeHtml(c.name)}</span></button>`;
  }).join("");
}

$("#spot-strip").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-id]");
  if (btn && btn.dataset.id !== state.spotlight) openSpotlight(btn.dataset.id);
});
$("#spot-back").addEventListener("click", closeSpotlight);
$("#spot-full").addEventListener("click", () => {
  const box = $("#spot-video");
  if (document.fullscreenElement) document.exitFullscreen();
  else box.requestFullscreen?.().catch(() => {});
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && state.spotlight && !$("#phone-dialog").open && !document.fullscreenElement) closeSpotlight();
});

// =============================================================== add-a-phone dialog
let phonesBeforeDialog = null;  // phone camera ids that existed when the dialog opened
let phoneLink = "";

// The link + QR code never change while the server runs, so they are loaded once when the
// page opens: the dialog then appears instantly instead of waiting for a round trip to the server.
let phoneInfo = null;
const phoneInfoReady = getJSON("/api/phone-link").then((info) => (phoneInfo = info)).catch(() => null);

async function openPhoneDialog() {
  const dialog = $("#phone-dialog");
  phonesBeforeDialog = new Set(state.cameras.filter((c) => c.kind === "phone").map((c) => c.id));
  setQrStatus(false);
  try {
    const info = phoneInfo || (await phoneInfoReady) || (await getJSON("/api/phone-link"));
    $("#dialog-off").classList.toggle("hidden", info.enabled);
    $("#dialog-on").classList.toggle("hidden", !info.enabled);
    $("#qr-status").classList.toggle("hidden", !info.enabled);
    const tile = $("#qr-tile");
    if (info.enabled) {
      tile.classList.remove("off");
      tile.innerHTML = info.qr_svg.slice(info.qr_svg.indexOf("<svg"));  // our own server's SVG
      phoneLink = info.link;
      $("#phone-link").value = info.link.replace(/#token=.*/, "#token=••••••••");
      const s = state.summary;
      const slots = s ? ` ${s.phones} of ${s.max_phones} phone slots in use.` : "";
      setHtml($("#dialog-note"), (info.has_token
        ? "The link only lets a phone <strong>send</strong> video. It can't open this dashboard."
        : "The phone will be asked for the phone token (set a dashboard password to put it in the link).") + slots);
    } else {
      tile.classList.add("off");
      tile.textContent = "Phone streaming is off";
    }
  } catch (e) {
    $("#qr-tile").textContent = "Could not load the link";
  }
  $("#btn-copy").textContent = "Copy";
  dialog.showModal();
}

function setQrStatus(name) {
  const el = $("#qr-status");
  el.classList.toggle("done", !!name);
  setHtml(el, name
    ? `${icon("check", "ic sm")}${escapeHtml(name)} connected`
    : '<span class="dot pending"></span>Waiting for a phone to scan…');
}

// While the dialog is open: a phone that starts streaming is announced, then the dialog closes.
function checkNewPhone() {
  const dialog = $("#phone-dialog");
  if (!dialog.open || !phonesBeforeDialog) return;
  const fresh = state.cameras.find((c) => c.kind === "phone" && c.status === "online" && !phonesBeforeDialog.has(c.id));
  if (fresh) {
    phonesBeforeDialog = null;
    setQrStatus(fresh.name);
    setTimeout(() => dialog.open && dialog.close(), 2000);
  }
}

$("#btn-add-phone").addEventListener("click", openPhoneDialog);
$("#dialog-close").addEventListener("click", () => $("#phone-dialog").close());
$("#phone-dialog").addEventListener("click", (ev) => {  // click on the dark backdrop closes it
  if (ev.target === ev.currentTarget) {
    const r = ev.currentTarget.getBoundingClientRect();
    const inside = ev.clientX >= r.left && ev.clientX <= r.right && ev.clientY >= r.top && ev.clientY <= r.bottom;
    if (!inside) ev.currentTarget.close();
  }
});
$("#btn-copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(phoneLink);
    $("#btn-copy").textContent = "Copied";
  } catch (e) {
    const input = $("#phone-link");
    input.value = phoneLink;
    input.select();
  }
});

// =============================================================== start
$("#host").textContent = location.host;
pollCameras().then(() => { pollHistory(); pollEvents(); });
refreshStills();
setInterval(pollCameras, POLL_STATS_MS);
setInterval(pollEvents, POLL_SLOW_MS);
setInterval(pollHistory, POLL_SLOW_MS);
