# Multi-Camera Monitoring Dashboard

Real-time monitoring of several camera feeds in one web dashboard, with video analytics:
camera status, FPS, motion detection, tampering detection (dark/blurry), people counting
(YOLOv8n), crowd alerts, history charts and an SQLite event log.

Built with Python, OpenCV, ultralytics YOLO, Flask, Docker; deployed on an Azure VM.
CSCI 436 Introduction to Cloud Computing final project.

## Run with Docker (recommended)

```bash
docker compose up -d --build     # build the image and start in the background
open http://localhost:8000       # dashboard
docker compose logs -f           # live logs
docker compose down              # stop
```

## Run locally without Docker (development)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.main     # http://localhost:8000
```

Test scripts:

```bash
.venv/bin/python -m scripts.run_workers 30     # step 1: video workers, FPS, offline detection
.venv/bin/python -m scripts.run_analytics 60   # step 2: analytics + every alert type + benchmark
```

## Stream live from a phone (iPhone / Android)

Besides video files, a camera can get its frames pushed from a phone over the network.
`config.yaml` has one such camera already (`cam5`, `source: push`).

1. On the phone open **`https://<server-ip>:8443/push`** in Safari (or Chrome).
2. The first time, the browser warns that the connection is not private (the server uses a
   self-signed certificate): tap *Show Details* → *visit this website*.
3. Allow camera access and tap **Start streaming**. The phone appears live on the dashboard
   as "iPhone Live", with the same analytics (people, motion, tampering, alerts) as the other cameras.

How it works: the page captures camera frames with `getUserMedia`, draws them on a canvas,
encodes JPEG and POSTs each one to `/api/push/<cam_id>`. The server keeps only the newest
frame per camera, and the camera worker processes it like any other source. Stopping the
stream (or losing the network) makes the camera go "offline" after `offline_after_s`.

Notes:
- **HTTPS is required**: phones only allow the camera on secure pages, so the app also listens
  on HTTPS port `8443` (`HTTPS_PORT`, `0` disables it). On Azure, allow inbound TCP **8443** in
  the VM's network security group, like port 8000.
- To use a real certificate instead of the self-signed one, set `CERT_FILE` and `KEY_FILE`.
- Set `PUSH_TOKEN=some-secret` (e.g. `PUSH_TOKEN=abc docker compose up -d`) so only people who
  know the token can push video; the phone page then asks for it.
- For more phones, add more cameras with `source: push` (cam6, cam7, ...) and pick one on the page.
- Keep Safari in the foreground with the phone unlocked: iOS pauses the camera in the background.

## Configuration

Everything is in [`config.yaml`](config.yaml): the camera list (id, name, video file, crowd limit)
and all analytics thresholds. In Docker the file is mounted into the container, so after editing
it just run `docker compose restart`, with no rebuild needed.

Environment variables: `PORT` (host port, default 8000), `HTTPS_PORT` (default 8443), `PUSH_TOKEN`,
`CERT_FILE` / `KEY_FILE`, `CONFIG_PATH`, `LOG_DIR`, `DATABASE`.

## Endpoints

| Path | Description |
|---|---|
| `/` | Dashboard |
| `/video/<cam_id>` | Live MJPEG stream |
| `/api/cameras` | Stats of all cameras + summary |
| `/api/history` | Last 5 min of people / motion per camera |
| `/api/events?limit=50` | Latest alerts |
| `/health` | Health check |
| `/push` | Phone page: stream the phone camera (open over HTTPS, port 8443) |
| `/api/push/<cam_id>` | POST one JPEG frame into a `source: push` camera |

## Project layout

```
app/            Flask server, camera workers, analytics, dashboard (templates/static)
scripts/        test scripts for steps 1 and 2
videos/         sample videos (credits in videos/SOURCES.md)
config.yaml     cameras + thresholds
logs/           real execution logs (submission)
results/        outputs: snapshots, benchmarks, events, screenshots
```
