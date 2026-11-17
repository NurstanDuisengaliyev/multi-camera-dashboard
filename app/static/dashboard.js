// Dashboard logic. Polls the JSON API and keeps the page up to date:
//   /api/cameras  every second  -> camera cards (added/removed as phones come and go), top tiles
//   /api/events   every 3 s     -> alerts panel
//   /api/history  every 3 s     -> charts
// The videos themselves are plain <img> tags showing MJPEG streams (no JS needed).

const POLL_STATS_MS = 1000;
const POLL_SLOW_MS = 3000;   // events + charts change slowly

const css = getComputedStyle(document.documentElement);
const color = (name) => css.getPropertyValue(name).trim();
const SERIES_COUNT = 8;      // --series-1 ... --series-8 in style.css

// Alert name -> label + severity (severity picks the color; the text label is always shown)
const ALERTS = {
  motion: { label: "Motion", level: "warning" },
  crowd: { label: "Crowd", level: "serious" },
  dark: { label: "Too dark", level: "critical" },
  blurry: { label: "Blurry", level: "critical" },
};
const EVENT_TYPES = {
  motion: { label: "Motion", level: "warning" },
  crowd: { label: "Crowd", level: "serious" },
  camera_dark: { label: "Camera dark", level: "critical" },
  camera_blurry: { label: "Camera blurry", level: "critical" },
  camera_offline: { label: "Camera offline", level: "critical" },
  camera_online: { label: "Camera online", level: "good" },
  phone_connected: { label: "Phone live", level: "good" },
  phone_disconnected: { label: "Phone stopped", level: "none" },
  system_start: { label: "System", level: "good" },
};
const STATUS_TEXT = { online: "Online", offline: "Offline", waiting: "Waiting for phone" };

const $ = (sel, root = document) => root.querySelector(sel);
// 24-hour clock everywhere (same format as the event log).
const fmtTime = (ms, withSeconds = true) => new Date(ms).toLocaleTimeString([], {
  hour: "2-digit", minute: "2-digit", second: withSeconds ? "2-digit" : undefined, hour12: false,
});
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function getJSON(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`${url}: ${res.status}`);
  return res.json();
}

function setConnection(ok) {
  $("#conn-banner").classList.toggle("hidden", ok);
}

// ---------------- camera cards ----------------
// cam_id -> { card, series }  (series = color number 1..8, kept while the camera exists)
const cards = new Map();

function freeSeries() {
  const used = new Set([...cards.values()].map((c) => c.series));
  for (let i = 1; i <= SERIES_COUNT; i++) if (!used.has(i)) return i;
  return (cards.size % SERIES_COUNT) + 1;
}

function addCard(cam) {
  const card = $("#cam-template").content.firstElementChild.cloneNode(true);
  const series = freeSeries();
  card.id = `card-${cam.id}`;
  $(".swatch", card).classList.add(`series-${series}`);
  $('[data-field="name"]', card).textContent = cam.name;
  $('[data-field="id"]', card).textContent = cam.kind === "phone" ? "📱 phone" : cam.id;
  const img = $("img", card);
  img.src = `/video/${encodeURIComponent(cam.id)}`;
  img.alt = `Live video from ${cam.name}`;
  $("#cams").appendChild(card);
  cards.set(cam.id, { card, series });
  rebuildChartSeries();
}

function removeCard(camId) {
  const { card } = cards.get(camId);
  $("img", card).src = "";  // closes the video stream connection
  card.remove();
  cards.delete(camId);
  rebuildChartSeries();
}

// Make the cards match the camera list from the server.
function syncCards(cameras) {
  const ids = new Set(cameras.map((c) => c.id));
  [...cards.keys()].filter((id) => !ids.has(id)).forEach(removeCard);
  cameras.filter((c) => !cards.has(c.id)).forEach(addCard);
}

function renderCamera(cam) {
  const { card } = cards.get(cam.id);
  const online = cam.status === "online";
  card.classList.toggle("offline", cam.status === "offline");
  card.classList.toggle("waiting", cam.status === "waiting");
  card.classList.toggle("has-alert", online && cam.alerts.length > 0);

  const set = (field, html) => { $(`[data-field="${field}"]`, card).innerHTML = html; };
  set("name", escapeHtml(cam.name));
  const status = $('[data-field="status"]', card);
  status.className = `status ${cam.status}`;
  $(".status-text", status).textContent = STATUS_TEXT[cam.status] || cam.status;

  if (online) {
    set("people", `${cam.people}<span class="limit"> / ${cam.crowd_limit}</span>`);
    set("motion", `${cam.motion_pct.toFixed(1)}%`);
    set("fps", cam.fps);
    set("brightness", cam.brightness);
    set("sharpness", cam.sharpness);
  } else {
    ["people", "motion", "fps", "brightness", "sharpness"].forEach((f) => set(f, "–"));
  }

  const badges = cam.alerts.map((a) => {
    const info = ALERTS[a] || { label: a, level: "warning" };
    return `<span class="badge ${info.level}">▲ ${info.label}</span>`;
  });
  if (cam.status === "offline") badges.push('<span class="badge critical">■ No signal</span>');
  if (cam.status === "waiting") badges.push('<span class="badge none">Phone not streaming</span>');
  set("alerts", badges.length ? badges.join("") : '<span class="badge none">No alerts</span>');
}

async function pollCameras() {
  try {
    const data = await getJSON("/api/cameras");
    syncCards(data.cameras);
    data.cameras.forEach(renderCamera);
    const s = data.summary;
    $("#sum-online").textContent = `${s.online} / ${s.total}`;
    $("#sum-people").textContent = s.people;
    $("#sum-alerts").textContent = s.active_alerts;
    $("#sum-alerts").classList.toggle("alerting", s.active_alerts > 0);
    $("#clock").textContent = fmtTime(data.server_time * 1000);
    setConnection(true);
  } catch (e) {
    setConnection(false);
  }
}

// ---------------- events panel ----------------
let newestEvent = null;

async function pollEvents() {
  try {
    const events = await getJSON("/api/events?limit=50");
    const list = $("#events");
    if (!events.length) return;
    const firstKey = (e) => `${e.ts}|${e.camera_id}|${e.type}`;
    const prevNewest = newestEvent;
    newestEvent = firstKey(events[0]);
    if (prevNewest === newestEvent) return; // nothing new

    let seenOld = prevNewest === null;
    list.innerHTML = events.map((e) => {
      const info = EVENT_TYPES[e.type] || { label: e.type, level: "warning" };
      const isNew = !seenOld && firstKey(e) !== prevNewest;
      if (firstKey(e) === prevNewest) seenOld = true;
      return `<li class="${isNew ? "new" : ""}">
        <span class="ev-time">${fmtTime(e.ts * 1000)}</span>
        <span class="ev-head"><span class="badge ${info.level}">${info.label}</span> ${escapeHtml(e.camera_name)}</span>
        <span class="ev-msg">${escapeHtml(e.message)}</span>
      </li>`;
    }).join("");
  } catch (e) { /* connection banner is handled by pollCameras */ }
}

// ---------------- charts ----------------
function makeChart(canvasId) {
  const grid = color("--border");
  const text = color("--text-2");
  return new Chart(document.getElementById(canvasId), {
    type: "line",
    data: { datasets: [] },
    options: {
      animation: false,
      maintainAspectRatio: false,
      parsing: false,
      // Hover anywhere on the chart -> tooltip with all cameras at that moment.
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { color: text, boxWidth: 10, boxHeight: 10, font: { size: 11 } } },
        tooltip: { callbacks: { title: (items) => fmtTime(items[0].parsed.x) } },
      },
      scales: {
        x: {
          type: "linear",
          ticks: {
            color: text, maxTicksLimit: 4, maxRotation: 0, font: { size: 10 },
            callback: (v) => fmtTime(v),
          },
          grid: { color: grid, drawTicks: false },
        },
        y: {
          beginAtZero: true,
          ticks: { color: text, font: { size: 10 }, precision: 0 },
          grid: { color: grid, drawTicks: false },
        },
      },
    },
  });
}

const peopleChart = makeChart("chart-people");
const motionChart = makeChart("chart-motion");

// One line per camera, in the camera's card color.
function rebuildChartSeries() {
  [peopleChart, motionChart].forEach((chart) => {
    const old = new Map(chart.data.datasets.map((d) => [d.camId, d.data]));
    chart.data.datasets = [...cards.entries()].map(([camId, { card, series }]) => ({
      camId,
      label: $('[data-field="name"]', card).textContent || camId,
      data: old.get(camId) || [],
      borderColor: color(`--series-${series}`),
      backgroundColor: color(`--series-${series}`),
      borderWidth: 2,
      pointRadius: 0,
      pointHoverRadius: 4,
      tension: 0.25,
    }));
    chart.update();
  });
}

async function pollHistory() {
  try {
    const history = await getJSON("/api/history");
    [[peopleChart, "people"], [motionChart, "motion_pct"]].forEach(([chart, field]) => {
      chart.data.datasets.forEach((d) => {
        d.label = $(`#card-${CSS.escape(d.camId)} [data-field="name"]`)?.textContent || d.camId;
        d.data = (history[d.camId] || []).map((r) => ({ x: r.t * 1000, y: r[field] }));
      });
      chart.update();
    });
  } catch (e) { /* ignore, retried on next poll */ }
}

// ---------------- start polling ----------------
pollCameras().then(pollHistory);
pollEvents();
setInterval(pollCameras, POLL_STATS_MS);
setInterval(pollEvents, POLL_SLOW_MS);
setInterval(pollHistory, POLL_SLOW_MS);
