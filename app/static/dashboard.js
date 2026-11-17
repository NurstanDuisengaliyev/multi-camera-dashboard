// Dashboard logic: the videos are plain <img> MJPEG streams (no JS needed).
// This file polls the JSON API and updates stats, alert badges, the events panel and charts.

const POLL_STATS_MS = 1000;
const POLL_SLOW_MS = 3000;   // events + charts change slowly

const css = getComputedStyle(document.documentElement);
const color = (name) => css.getPropertyValue(name).trim();
// One fixed color per camera (same order as the swatches on the cards). Up to 8 cameras.
const SERIES = [1, 2, 3, 4, 5, 6, 7, 8].map((i) => color(`--series-${i}`));

// Alert name -> label + severity (severity picks the status color; the text label is always shown)
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
  system_start: { label: "System", level: "good" },
};

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

// ---------------- camera stats ----------------
function renderCamera(cam) {
  const card = document.getElementById(`card-${cam.id}`);
  if (!card) return;
  const online = cam.status === "online";
  card.classList.toggle("offline", !online);
  card.classList.toggle("has-alert", online && cam.alerts.length > 0);

  const status = $('[data-field="status"]', card);
  status.className = `status ${cam.status}`;
  $(".status-text", status).textContent = online ? "Online" : "Offline";

  const set = (field, html) => { $(`[data-field="${field}"]`, card).innerHTML = html; };
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
  if (!online) badges.push('<span class="badge critical">■ No signal</span>');
  set("alerts", badges.length ? badges.join("") : '<span class="badge none">No alerts</span>');
}

async function pollCameras() {
  try {
    const data = await getJSON("/api/cameras");
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
    const firstKey = (e) => `${e.time}|${e.camera_id}|${e.type}`;
    const prevNewest = newestEvent;
    newestEvent = firstKey(events[0]);
    if (prevNewest === newestEvent) return; // nothing new

    let seenOld = prevNewest === null;
    list.innerHTML = events.map((e) => {
      const info = EVENT_TYPES[e.type] || { label: e.type, level: "warning" };
      const isNew = !seenOld && firstKey(e) !== prevNewest;
      if (firstKey(e) === prevNewest) seenOld = true;
      return `<li class="${isNew ? "new" : ""}">
        <span class="ev-time">${escapeHtml(e.time.slice(11))}</span>
        <span class="ev-head"><span class="badge ${info.level}">${info.label}</span> ${escapeHtml(e.camera_name)}</span>
        <span class="ev-msg">${escapeHtml(e.message)}</span>
      </li>`;
    }).join("");
  } catch (e) { /* connection banner is handled by pollCameras */ }
}

// ---------------- charts ----------------
function makeChart(canvasId, yTitle) {
  const grid = color("--border");
  const text = color("--text-2");
  return new Chart(document.getElementById(canvasId), {
    type: "line",
    data: {
      datasets: window.CAMERAS.map((cam, i) => ({
        label: cam.name,
        data: [],
        borderColor: SERIES[i % SERIES.length],
        backgroundColor: SERIES[i % SERIES.length],
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 4,
        tension: 0.25,
      })),
    },
    options: {
      animation: false,
      maintainAspectRatio: false,
      parsing: false,
      // Hover anywhere on the chart -> tooltip with all cameras at that moment.
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { color: text, boxWidth: 10, boxHeight: 10, font: { size: 11 } } },
        tooltip: {
          callbacks: {
            title: (items) => fmtTime(items[0].parsed.x),
          },
        },
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
          title: { display: false, text: yTitle },
          ticks: { color: text, font: { size: 10 }, precision: 0 },
          grid: { color: grid, drawTicks: false },
        },
      },
    },
  });
}

const peopleChart = makeChart("chart-people", "people");
const motionChart = makeChart("chart-motion", "% of image");

async function pollHistory() {
  try {
    const history = await getJSON("/api/history");
    window.CAMERAS.forEach((cam, i) => {
      const rows = history[cam.id] || [];
      peopleChart.data.datasets[i].data = rows.map((r) => ({ x: r.t * 1000, y: r.people }));
      motionChart.data.datasets[i].data = rows.map((r) => ({ x: r.t * 1000, y: r.motion_pct }));
    });
    peopleChart.update();
    motionChart.update();
  } catch (e) { /* ignore, retried on next poll */ }
}

// ---------------- start polling ----------------
pollCameras();
pollEvents();
pollHistory();
setInterval(pollCameras, POLL_STATS_MS);
setInterval(pollEvents, POLL_SLOW_MS);
setInterval(pollHistory, POLL_SLOW_MS);
