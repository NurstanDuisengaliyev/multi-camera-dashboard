// Phone streaming page: opens the phone camera, and in a loop
//   draw the current video frame on a canvas -> encode JPEG -> POST /api/push/<cam_id>.
// The next frame is sent only after the server answered the previous one, so a slow
// network lowers the FPS instead of building up delay (always shows "now").

const $ = (id) => document.getElementById(id);
const video = $("preview");
const canvas = document.createElement("canvas");
const ctx = canvas.getContext("2d");

let stream = null;        // MediaStream from the camera
let facing = "environment"; // "environment" = back camera, "user" = front (selfie)
let running = false;
let wakeLock = null;
let sentTimes = [];       // timestamps of frames sent in the last second (for FPS)

const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
};

function setStatus(text, level = "") {
  const el = $("st-status");
  el.textContent = text;
  el.className = level;
}

// ---------------- camera ----------------
async function openCamera() {
  closeCamera();
  stream = await navigator.mediaDevices.getUserMedia({
    audio: false,
    video: { facingMode: { ideal: facing }, width: { ideal: 1280 }, height: { ideal: 720 },
             frameRate: { ideal: 30 } },
  });
  video.srcObject = stream;
  await video.play();
  $("placeholder").classList.add("hidden");
  // iOS stops the camera when Safari goes to the background; reopen it when we come back.
  stream.getVideoTracks()[0].addEventListener("ended", () => { if (running) setStatus("Camera stopped", "bad"); });
}

function closeCamera() {
  if (stream) stream.getTracks().forEach((t) => t.stop());
  stream = null;
  video.srcObject = null;
}

// ---------------- sending ----------------
function grabJpeg() {
  const vw = video.videoWidth, vh = video.videoHeight;
  if (!vw || !vh) return Promise.resolve(null);
  const w = Math.min(Number($("width").value), vw);
  const h = Math.round(vh * w / vw);
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  ctx.drawImage(video, 0, 0, w, h);
  return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", Number($("quality").value)));
}

async function sendLoop() {
  while (running) {
    const started = performance.now();
    const interval = 1000 / Number($("fps").value);
    try {
      const blob = await grabJpeg();
      if (blob) {
        const headers = { "Content-Type": "image/jpeg" };
        if (window.PUSH.needToken) headers["X-Push-Token"] = $("token").value;
        const t0 = performance.now();
        const res = await fetch(`/api/push/${encodeURIComponent($("cam").value)}`,
                                { method: "POST", body: blob, headers, cache: "no-store" });
        const rtt = performance.now() - t0;
        if (res.status === 403 || res.status === 404) {
          const err = await res.json().catch(() => ({}));
          stop(err.error || `Server refused (${res.status})`);
          return;
        }
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const now = performance.now();
        sentTimes.push(now);
        sentTimes = sentTimes.filter((t) => now - t <= 1000);
        $("st-fps").textContent = sentTimes.length;
        $("st-size").textContent = `${canvas.width}×${canvas.height} · ${Math.round(blob.size / 1024)} KB`;
        $("st-rtt").textContent = `${Math.round(rtt)} ms`;
        setStatus("Streaming", "good");
      }
    } catch (e) {
      // Network hiccup: keep trying, the dashboard shows "offline" after a few seconds.
      setStatus("Reconnecting…", "bad");
      await sleep(1000);
    }
    await sleep(Math.max(0, interval - (performance.now() - started)));
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------- start / stop ----------------
async function start() {
  if (window.PUSH.needToken && !$("token").value) {
    setStatus("Enter the push token", "bad");
    $("token").focus();
    return;
  }
  try {
    setStatus("Opening camera…");
    if (!stream) await openCamera();
  } catch (e) {
    setStatus(e.name === "NotAllowedError" ? "Camera permission denied" : `Camera error: ${e.message}`, "bad");
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
  running = false;
  $("btn-start").textContent = "Start streaming";
  $("btn-start").classList.remove("stop");
  $("live-badge").classList.add("hidden");
  $("st-fps").textContent = "–";
  setStatus(reason || "Stopped", reason ? "bad" : "");
  if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; }
}

async function keepScreenOn() {
  // Screen Wake Lock (iOS 16.4+): the phone doesn't auto-lock while streaming.
  try { if ("wakeLock" in navigator) wakeLock = await navigator.wakeLock.request("screen"); } catch (e) { /* not allowed */ }
}

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
  store.set("push.facing", facing);
  if (stream) {
    try { await openCamera(); } catch (e) { setStatus(`Camera error: ${e.message}`, "bad"); }
  }
});

// ---------------- settings remembered on this phone ----------------
facing = store.get("push.facing", facing);
["cam", "width", "fps", "quality", "token"].forEach((id) => {
  const el = $(id);
  if (!el) return;
  const saved = store.get(`push.${id}`, null);
  if (saved !== null && (el.tagName !== "SELECT" || [...el.options].some((o) => o.value === saved))) el.value = saved;
  el.addEventListener("change", () => store.set(`push.${id}`, el.value));
});

// ---------------- HTTPS check ----------------
if (!window.isSecureContext || !navigator.mediaDevices) {
  const port = window.PUSH.httpsPort ? `:${window.PUSH.httpsPort}` : "";
  const url = `https://${location.hostname}${port}${location.pathname}`;
  $("https-link").href = url;
  $("insecure").classList.remove("hidden");
  $("btn-start").disabled = true;
  $("btn-flip").disabled = true;
  setStatus("Needs HTTPS", "bad");
}
