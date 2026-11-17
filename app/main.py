"""Flask web server: starts the camera workers and serves the dashboard.

Routes:
  /                      dashboard page (HTML)
  /video/<cam_id>        live MJPEG stream of one camera (used by <img> tags)
  /api/cameras           JSON: stats of all cameras + summary
  /api/history           JSON: last 5 minutes of people/motion per camera (charts)
  /api/events?limit=50   JSON: latest alerts from the SQLite event log
  /health                JSON: is the service alive? (Docker healthcheck / deployment proof)

Run locally:   python -m app.main
In Docker:     gunicorn "app.main:create_app()"   (see Dockerfile)
"""
import logging
import os
import time

import cv2
import numpy as np
from flask import Flask, Response, abort, jsonify, render_template, request

from app.analytics.people import PeopleDetector
from app.config import load_config, setup_logging
from app.events import EventStore
from app.worker import CameraWorker

log = logging.getLogger("server")


def no_signal_jpeg(text):
    """A dark 'NO SIGNAL' image shown when a camera is offline."""
    img = np.full((360, 640, 3), 30, np.uint8)
    cv2.putText(img, "NO SIGNAL", (190, 170), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (60, 60, 210), 3, cv2.LINE_AA)
    cv2.putText(img, text, (20, 220), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1, cv2.LINE_AA)
    return cv2.imencode(".jpg", img)[1].tobytes()


def mjpeg_stream(worker, fps, quality):
    """Generator that never ends: yields one JPEG 'part' per new frame.
    The browser replaces the picture with each part -> looks like video."""
    interval = 1.0 / fps
    placeholder = no_signal_jpeg("%s (%s)" % (worker.cam_name, worker.cam_id))
    last_sent = -1
    while True:
        if worker.get_stats()["status"] == "offline":
            jpeg, frame_no = placeholder, -1
        else:
            jpeg, frame_no = worker.get_jpeg(quality)
        if jpeg is not None and (frame_no != last_sent or frame_no == -1):
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
            last_sent = frame_no
        # Limit the stream FPS: saves bandwidth (cloud traffic costs money) and CPU.
        time.sleep(interval if frame_no != -1 else 1.0)


def create_app():
    setup_logging("app.log")
    config = load_config()
    a = config["analytics"]
    server_cfg = config.get("server", {})
    stream_fps = server_cfg.get("stream_fps", 15)
    jpeg_quality = server_cfg.get("jpeg_quality", 75)

    # Shared services, then one worker thread per camera.
    events = EventStore(os.environ.get("DATABASE", config.get("database", "data/events.db")),
                        cooldown_s=a.get("alert_cooldown_s", 30))
    detector = PeopleDetector(a.get("model", "yolov8n.pt"), a.get("person_confidence", 0.4))
    workers = {}
    for cam in config["cameras"]:
        workers[cam["id"]] = CameraWorker(cam, a, detector, events)
        workers[cam["id"]].start()
    started_at = time.time()
    log.info("started %d camera workers: %s", len(workers), ", ".join(workers))
    events.add("system", "System", "system_start", "Monitoring started with %d cameras" % len(workers),
               cooldown=False)

    app = Flask(__name__)

    @app.route("/")
    def index():
        cams = [{"id": w.cam_id, "name": w.cam_name} for w in workers.values()]
        return render_template("index.html", cameras=cams)

    @app.route("/video/<cam_id>")
    def video(cam_id):
        worker = workers.get(cam_id) or abort(404)
        return Response(mjpeg_stream(worker, stream_fps, jpeg_quality),
                        mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.route("/api/cameras")
    def api_cameras():
        cams = [w.get_stats() for w in workers.values()]
        summary = {
            "total": len(cams),
            "online": sum(c["status"] == "online" for c in cams),
            "people": sum(c["people"] for c in cams if c["status"] == "online"),
            "active_alerts": sum(len(c["alerts"]) for c in cams),
        }
        return jsonify({"cameras": cams, "summary": summary, "server_time": time.time()})

    @app.route("/api/history")
    def api_history():
        return jsonify({cam_id: w.get_history() for cam_id, w in workers.items()})

    @app.route("/api/events")
    def api_events():
        limit = min(int(request.args.get("limit", 50)), 500)
        return jsonify(events.recent(limit))

    @app.route("/health")
    def health():
        online = sum(w.get_stats()["status"] == "online" for w in workers.values())
        return jsonify({"status": "ok", "cameras_online": online, "cameras_total": len(workers),
                        "uptime_s": round(time.time() - started_at)})

    return app


if __name__ == "__main__":
    app = create_app()
    # 0.0.0.0 = accept connections from other machines too (needed in Docker / on Azure).
    # threaded=True: every browser connection (each video stream) gets its own thread.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), threaded=True)
