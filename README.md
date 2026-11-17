# Multi-Camera Monitoring Dashboard

Real-time monitoring of several camera feeds in one web dashboard, with video analytics:
camera status, FPS, motion detection, tampering detection (dark/blurry), people counting
(YOLOv8n), crowd alerts, history charts and an SQLite event log.

Built with Python, OpenCV, ultralytics YOLO, Flask, Docker; deployed on an Azure VM.
CSCI 436 Introduction to Cloud Computing final project. Full plan: [PLAN.md](PLAN.md).

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

## Configuration

Everything is in [`config.yaml`](config.yaml): the camera list (id, name, video file, crowd limit)
and all analytics thresholds. In Docker the file is mounted into the container, so after editing
it just run `docker compose restart`, with no rebuild needed.

Environment variables: `PORT` (host port, default 8000), `CONFIG_PATH`, `LOG_DIR`, `DATABASE`.

## Endpoints

| Path | Description |
|---|---|
| `/` | Dashboard |
| `/video/<cam_id>` | Live MJPEG stream |
| `/api/cameras` | Stats of all cameras + summary |
| `/api/history` | Last 5 min of people / motion per camera |
| `/api/events?limit=50` | Latest alerts |
| `/health` | Health check |

## Project layout

```
app/            Flask server, camera workers, analytics, dashboard (templates/static)
scripts/        test scripts for steps 1 and 2
videos/         sample videos (credits in videos/SOURCES.md)
config.yaml     cameras + thresholds
logs/           real execution logs (submission)
results/        outputs: snapshots, benchmarks, events, screenshots
```
