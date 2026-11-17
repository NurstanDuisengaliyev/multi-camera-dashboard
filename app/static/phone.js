// Phone streaming page.
//   1. Start: POST /api/phone/start {name}  -> the server creates a camera and returns its id
//   2. Loop:  draw the current video frame on a canvas -> JPEG -> POST /api/phone/<id>/frame
//   3. Stop:  POST /api/phone/<id>/stop     -> the camera disappears from the dashboard
// The next frame is sent only after the server answered the previous one, so a slow
// network lowers the FPS instead of building up delay (the dashboard always shows "now").

const $ = (id) => document.getElementById(id);
const video = $("preview");
const canvas = document.createElement("canvas");
const ctx = canvas.getContext("2d");

let stream = null;          // MediaStream from the camera
let facing = "environment"; // "environment" = back camera, "user" = front (selfie)
let running = false;
let cameraId = null;        // id the server gave us
let wakeLock = null;
let sentTimes = [];         // when the frames of the last second were sent (for FPS)

// Settings are remembered on this phone (wrapped: storage can be blocked in private mode).
const store = {
  get(k, d) { try { return localStorage.getItem(`phone.${k}`) ?? d; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(`phone.${k}`, v); } catch (e) { /* ignore */ } },
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function setStatus(text, level = "") {
  $("st-status").textContent = text;
  $("st-status").className = level;
}

// ---------------- talking to the server ----------------
class ServerError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(path, body, contentType = "application/json") {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": contentType, "X-Phone-Token": $("token").value.trim() },
    body: contentType === "application/json" ? JSON.stringify(body) : body,
    cache: "no-store",
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new ServerError(res.status, data.error || `Server error ${res.status}`);
  }
  return res.json();
}

async function register() {
  const data = await api("/api/phone/start", { name: $("name").value, camera_id: store.get("cameraId", null) });
  cameraId = data.camera_id;
  store.set("cameraId", cameraId);  // reconnecting later keeps the same camera on the dashboard
}

// ---------------- camera ----------------
async function openCamera() {
  closeCamera();
  stream = await navigator.mediaDevices.getUserMedia({
    audio: false,
    video: { facingMode: { ideal: facing }, width: { ideal: 1280 }, height: { ideal: 720 } },
  });
  video.srcObject = stream;
  await video.play();
  $("placeholder").classList.add("hidden");
}

function closeCamera() {
  if (stream) stream.getTracks().forEach((t) => t.stop());
  stream = null;
  video.srcObject = null;
  $("placeholder").classList.remove("hidden");
}

function grabJpeg() {
  const vw = video.videoWidth, vh = video.videoHeight;
  if (!vw || !vh) return Promise.resolve(null);
  const w = Math.min(Number($("width").value), vw);
  const h = Math.round(vh * w / vw);
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  ctx.drawImage(video, 0, 0, w, h);
  return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", Number($("quality").value)));
}

// ---------------- sending loop ----------------
async function sendLoop() {
  while (running) {
    const started = performance.now();
    try {
      const jpeg = await grabJpeg();
      if (jpeg) {
        const t0 = performance.now();
        await api(`/api/phone/${cameraId}/frame`, jpeg, "image/jpeg");
        showStats(jpeg, performance.now() - t0);
      }
    } catch (e) {
      if (e.status === 404) {
        // Server forgot this camera (restart, or we were silent too long): register again.
        try { await register(); } catch (e2) { await handleError(e2); }
      } else {
        await handleError(e);
      }
    }
    const interval = 1000 / Number($("fps").value);
    await sleep(Math.max(0, interval - (performance.now() - started)));
  }
}

function showStats(jpeg, rttMs) {
  const now = performance.now();
  sentTimes = sentTimes.filter((t) => now - t < 1000);
  sentTimes.push(now);
  $("st-fps").textContent = sentTimes.length;
  $("st-size").textContent = `${canvas.width}×${canvas.height} · ${Math.round(jpeg.size / 1024)} KB`;
  $("st-rtt").textContent = `${Math.round(rttMs)} ms`;
  setStatus("Streaming", "good");
}

async function handleError(e) {
  if (e instanceof ServerError && [403, 429, 503].includes(e.status)) {
    stop(e.message);  // wrong token / too many phones / switched off: retrying won't help
  } else {
    setStatus("Reconnecting…", "bad");  // network hiccup: keep trying
    await sleep(1000);
  }
}

// ---------------- start / stop ----------------
async function start() {
  if (!$("token").value.trim()) {
    setStatus("Enter the phone token", "bad");
    $("token").focus();
    return;
  }
  if (!$("name").value.trim()) $("name").value = "Phone";
  try {
    setStatus("Opening camera…");
    if (!stream) await openCamera();
    setStatus("Connecting…");
    await register();
  } catch (e) {
    if (e.name === "NotAllowedError") setStatus("Camera permission denied", "bad");
    else setStatus(e.message, "bad");
    return;
  }
  running = true;
  $("btn-start").textContent = "Stop streaming";
  $("btn-start").classList.add("stop");
  $("live-badge").classList.remove("hidden");
  keepScreenOn();
  sendLoop();
}

function stop(reason) {
  const wasRunning = running;
  running = false;
  $("btn-start").textContent = "Start streaming";
  $("btn-start").classList.remove("stop");
  $("live-badge").classList.add("hidden");
  $("st-fps").textContent = "–";
  setStatus(reason || "Stopped", reason ? "bad" : "");
  if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; }
  // Tapped "Stop": remove the camera from the dashboard right away.
  if (wasRunning && !reason && cameraId) api(`/api/phone/${cameraId}/stop`, {}).catch(() => {});
}

async function keepScreenOn() {
  // Screen Wake Lock (iOS 16.4+, Android Chrome): the phone doesn't auto-lock while streaming.
  try { if ("wakeLock" in navigator) wakeLock = await navigator.wakeLock.request("screen"); } catch (e) { /* not allowed */ }
}

// Coming back to the page: the phone may have stopped the camera in the background.
document.addEventListener("visibilitychange", async () => {
  if (document.visibilityState !== "visible" || !running) return;
  keepScreenOn();
  const track = stream && stream.getVideoTracks()[0];
  if (!track || track.readyState === "ended") {
    try { await openCamera(); } catch (e) { stop("Camera could not be reopened"); }
  }
});

$("btn-start").addEventListener("click", () => (running ? stop() : start()));
$("btn-flip").addEventListener("click", async () => {
  facing = facing === "environment" ? "user" : "environment";
  store.set("facing", facing);
  if (stream) {
    try { await openCamera(); } catch (e) { setStatus(`Camera error: ${e.message}`, "bad"); }
  }
});

// ---------------- page setup ----------------
// A link from the dashboard looks like /phone#token=XYZ. Take the token, then remove it
// from the address bar (the part after '#' is never sent to the server).
const fromLink = new URLSearchParams(location.hash.slice(1)).get("token");
if (fromLink) {
  store.set("token", fromLink);
  history.replaceState(null, "", location.pathname);
}

facing = store.get("facing", facing);
["name", "token", "width", "fps", "quality"].forEach((id) => {
  const el = $(id);
  const saved = store.get(id, null);
  if (saved !== null && (el.tagName !== "SELECT" || [...el.options].some((o) => o.value === saved))) el.value = saved;
  el.addEventListener("change", () => store.set(id, el.value));
});

if (!window.isSecureContext || !navigator.mediaDevices) {
  $("https-link").href = `https://${location.hostname}${location.pathname}`;
  $("insecure").classList.remove("hidden");
  $("btn-start").disabled = true;
  $("btn-flip").disabled = true;
  setStatus("Needs HTTPS", "bad");
}
