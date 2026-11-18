// Phone streaming page.
//   1. Start: POST /api/phone/start {name}  -> the server creates a camera and returns its id
//   2. Loop:  draw the current video frame on a canvas -> JPEG -> POST /api/phone/<id>/frame
//   3. Stop:  POST /api/phone/<id>/stop     -> the camera disappears from the dashboard
// Up to MAX_IN_FLIGHT frames are on the way at the same time. With only one, the FPS could
// never be higher than 1 / round trip: phone -> cloud VM and back takes ~300 ms on mobile data,
// which gave only ~3 fps. If the network can't keep up, new frames are skipped (never queued),
// so the delay stays low. Each frame carries its capture time (X-Phone-Seq): a frame that
// arrives after a newer one is dropped by the server, so the picture never jumps backwards.
const MAX_IN_FLIGHT = 3;

const $ = (id) => document.getElementById(id);
const video = $("preview");
const canvas = document.createElement("canvas");
const ctx = canvas.getContext("2d");

const QUALITY = {
  saver: { width: 480, fps: 8, jpeg: 0.6 },
  balanced: { width: 640, fps: 15, jpeg: 0.7 },
  sharp: { width: 960, fps: 15, jpeg: 0.8 },
};

let stream = null;          // MediaStream from the camera
let facing = "environment"; // "environment" = back camera, "user" = front (selfie)
let quality = "balanced";
let running = false;
let cameraId = null;        // id the server gave us
let lastRtt = null;         // round trip of the previous upload, sent with the next one
let startedAt = 0;
let timer = null;
let wakeLock = null;
let sentTimes = [];         // when the frames of the last second were sent (for FPS)

// Settings are remembered on this phone (wrapped: storage can be blocked in private mode).
const store = {
  get(k, d) { try { return localStorage.getItem(`phone.${k}`) ?? d; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(`phone.${k}`, v); } catch (e) { /* ignore */ } },
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function setStatus(text, bad = false) {
  $("status-line").textContent = text;
  $("status-line").className = `status-line${bad ? " bad" : ""}`;
}
function toast(text) {
  $("toast").textContent = text;
  $("toast").classList.toggle("hidden", !text);
}

// ---------------- talking to the server ----------------
class ServerError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(path, body, contentType = "application/json", extraHeaders = {}) {
  const headers = { "Content-Type": contentType, "X-Phone-Token": $("token").value.trim(), ...extraHeaders };
  if (lastRtt !== null) headers["X-Phone-RTT"] = String(lastRtt);
  const res = await fetch(path, {
    method: "POST", headers, cache: "no-store",
    body: contentType === "application/json" ? JSON.stringify(body) : body,
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
  $("live-name").textContent = data.name;
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

async function flipCamera() {
  facing = facing === "environment" ? "user" : "environment";
  store.set("facing", facing);
  try { await openCamera(); } catch (e) { setStatus(`Camera error: ${e.message}`, true); }
}

function grabJpeg() {
  const vw = video.videoWidth, vh = video.videoHeight;
  if (!vw || !vh) return Promise.resolve(null);
  const q = QUALITY[quality];
  const w = Math.min(q.width, vw);
  const h = Math.round(vh * w / vw);
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  ctx.drawImage(video, 0, 0, w, h);
  return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", q.jpeg));
}

// ---------------- sending loop ----------------
let inFlight = 0;           // uploads that haven't been answered yet
let pausedUntil = 0;        // after an error: wait a moment before sending again
let registering = null;     // a running re-register, so parallel uploads don't all start one

async function sendLoop() {
  while (running) {
    const started = performance.now();
    if (inFlight < MAX_IN_FLIGHT && started >= pausedUntil) {
      const seq = Date.now();  // capture time: always increases, even after a page reload
      const jpeg = await grabJpeg();
      if (jpeg) sendFrame(jpeg, seq);  // not awaited: the next frame can go out meanwhile
    }
    await sleep(Math.max(0, 1000 / QUALITY[quality].fps - (performance.now() - started)));
  }
}

async function sendFrame(jpeg, seq) {
  inFlight++;
  const t0 = performance.now();
  try {
    await api(`/api/phone/${cameraId}/frame`, jpeg, "image/jpeg", { "X-Phone-Seq": String(seq) });
    lastRtt = Math.round(performance.now() - t0);
    showStats(jpeg);
    toast("");
  } catch (e) {
    if (!running) return;
    pausedUntil = performance.now() + 1000;
    if (e.status === 404) {
      // The server forgot this camera (restart, or we were silent too long): register again.
      registering = registering || register().catch(handleError).finally(() => { registering = null; });
      await registering;
    } else {
      await handleError(e);
    }
  } finally {
    inFlight--;
  }
}

function showStats(jpeg) {
  const now = performance.now();
  sentTimes = sentTimes.filter((t) => now - t < 1000);
  sentTimes.push(now);
  $("st-fps").textContent = sentTimes.length;
  $("st-rtt").textContent = `${lastRtt} ms`;
  $("st-size").textContent = `${Math.round(jpeg.size / 1024)} KB`;
}

async function handleError(e) {
  if (e instanceof ServerError && [403, 429, 503].includes(e.status)) {
    stop(e.message);  // wrong token / too many phones / switched off: retrying won't help
  } else {
    toast("Connection problem, reconnecting…");  // network hiccup: keep trying
    await sleep(1000);
  }
}

// ---------------- start / stop ----------------
async function start() {
  if (!$("token").value.trim()) {
    showTokenField(true);
    setStatus("Enter the phone token", true);
    $("token").focus();
    return;
  }
  if (!$("name").value.trim()) $("name").value = "Phone";
  store.set("name", $("name").value);
  store.set("token", $("token").value.trim());
  try {
    setStatus("Opening camera…");
    if (!stream) await openCamera();
    setStatus("Connecting…");
    await register();
  } catch (e) {
    setStatus(e.name === "NotAllowedError" ? "Camera access was denied. Allow it in the browser settings." : e.message, true);
    return;
  }
  setStatus("");
  running = true;
  setLiveUi(true);
  keepScreenOn();
  sendLoop();
}

function stop(reason) {
  const userStopped = running && !reason;
  running = false;
  setLiveUi(false);
  setStatus(reason || "Stopped. Your camera is removed from the dashboard.", !!reason);
  if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; }
  // Tapped "Stop": remove the camera from the dashboard right away.
  if (userStopped && cameraId) api(`/api/phone/${cameraId}/stop`, {}).catch(() => {});
}

function setLiveUi(live) {
  $("sheet").classList.toggle("hidden", live);
  $("sheet").classList.remove("settings");
  $("live-bar").classList.toggle("hidden", !live);
  $("live-pill").classList.toggle("hidden", !live);
  $("host-pill").classList.toggle("hidden", live);
  $("btn-flip-top").classList.toggle("hidden", live);
  $("live-banner").classList.toggle("hidden", !live);
  toast("");
  clearInterval(timer);
  if (live) {
    startedAt = Date.now();
    sentTimes = [];
    const tick = () => {
      const s = Math.floor((Date.now() - startedAt) / 1000);
      $("live-timer").textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
    };
    tick();
    timer = setInterval(tick, 1000);
  }
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
    try { await openCamera(); } catch (e) { stop("The camera could not be reopened"); }
  }
});

// ---------------- controls ----------------
function setQuality(q) {
  quality = QUALITY[q] ? q : "balanced";
  store.set("quality", quality);
  document.querySelectorAll("#quality button").forEach((b) =>
    b.setAttribute("aria-checked", String(b.dataset.q === quality)));
}

function showTokenField(show) {
  $("token-field").classList.toggle("hidden", !show);
  $("token-ok").classList.toggle("hidden", show);
}

document.querySelectorAll("#quality button").forEach((b) => b.addEventListener("click", () => setQuality(b.dataset.q)));
$("btn-start").addEventListener("click", start);
$("btn-stop").addEventListener("click", () => stop());
$("btn-flip").addEventListener("click", flipCamera);
$("btn-flip-top").addEventListener("click", flipCamera);
$("btn-settings").addEventListener("click", () => {
  $("sheet").classList.add("settings");
  $("sheet").classList.remove("hidden");
});
$("btn-done").addEventListener("click", () => {
  $("sheet").classList.remove("settings");
  $("sheet").classList.add("hidden");
});
$("btn-change-token").addEventListener("click", () => { showTokenField(true); $("token").focus(); });

// ---------------- page setup ----------------
// A link from the dashboard looks like /phone#token=XYZ. Take the token, then remove it
// from the address bar (the part after '#' is never sent to the server).
const fromLink = new URLSearchParams(location.hash.slice(1)).get("token");
if (fromLink) {
  store.set("token", fromLink);
  history.replaceState(null, "", location.pathname);
}
$("token").value = store.get("token", "");
showTokenField(!$("token").value);
$("name").value = store.get("name", "");
facing = store.get("facing", facing);
setQuality(store.get("quality", "balanced"));
$("host").textContent = location.host;

if (!window.isSecureContext || !navigator.mediaDevices) {
  $("https-link").href = `https://${location.hostname}${location.pathname}`;
  $("insecure").classList.remove("hidden");
  $("btn-start").disabled = true;
  $("btn-flip-top").disabled = true;
} else if (window.PHONE_ENABLED) {
  // Show the preview right away, like a camera app (the browser asks for permission once).
  openCamera().catch((e) => {
    if (e.name === "NotAllowedError") setStatus("Camera access was denied. Allow it in the browser settings.", true);
  });
}
