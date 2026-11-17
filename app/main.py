"""Flask web server: starts the camera workers and serves the dashboard.

Routes:
  /                      dashboard page (HTML)
  /video/<cam_id>        live MJPEG stream of one camera (used by <img> tags)
  /api/cameras           JSON: stats of all cameras + summary
  /api/history           JSON: last 5 minutes of people/motion per camera (charts)
  /api/events?limit=50   JSON: latest alerts from the SQLite event log
  /health                JSON: is the service alive? (Docker healthcheck / deployment proof)
  /push                  phone page: stream the phone camera into a "source: push" camera
  /api/push/<cam_id>     POST one JPEG frame (body = image/jpeg), used by /push

The phone camera only works over HTTPS, so a second HTTPS listener is started on
HTTPS_PORT (default 8443, "0" = off). See app/tls.py.

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
from app.tls import start_https_server
from app.events import EventStore
from app.sources import is_push, push_hub
from app.worker import CameraWorker

MAX_PUSH_FRAME_BYTES = 2 * 1024 * 1024  # a 1280px JPEG is ~100-300 KB; refuse anything huge

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
    db_path = os.environ.get("DATABASE", config.get("database", "data/events.db"))
    events = EventStore(db_path, cooldown_s=a.get("alert_cooldown_s", 30))
    detector = PeopleDetector(a.get("model", "yolov8n.pt"), a.get("person_confidence", 0.4))
    workers = {}
    for cam in config["cameras"]:
        workers[cam["id"]] = CameraWorker(cam, a, detector, events)
        workers[cam["id"]].start()
    started_at = time.time()
    log.info("started %d camera workers: %s", len(workers), ", ".join(workers))
    events.add("system", "System", "system_start", "Monitoring started with %d cameras" % len(workers),
               cooldown=False)

    # Cameras the phone may stream into. Optional PUSH_TOKEN env var = a password for
    # pushing, so strangers who find the public IP can't replace your camera image.
    push_cams = [w for w in workers.values() if is_push(w.source_spec)]
    push_token = os.environ.get("PUSH_TOKEN", "")
    https_port = int(os.environ.get("HTTPS_PORT", 8443))

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_PUSH_FRAME_BYTES

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

    @app.route("/push")
    def push_page():
        cams = [{"id": w.cam_id, "name": w.cam_name} for w in push_cams]
        return render_template("push.html", cameras=cams, need_token=bool(push_token),
                               secure=request.is_secure, https_port=https_port)

    @app.route("/api/push/<cam_id>", methods=["POST"])
    def api_push(cam_id):
        if cam_id not in {w.cam_id for w in push_cams}:
            return jsonify({"error": "camera %s does not accept pushed video" % cam_id}), 404
        if push_token and request.headers.get("X-Push-Token", "") != push_token:
            return jsonify({"error": "wrong push token"}), 403
        jpeg = request.get_data()
        if not jpeg.startswith(b"\xff\xd8"):  # every JPEG file starts with these two bytes
            return jsonify({"error": "body must be a JPEG image"}), 400
        seq = push_hub.put(cam_id, jpeg)
        return jsonify({"ok": True, "frame": seq})

    @app.route("/health")
    def health():
        online = sum(w.get_stats()["status"] == "online" for w in workers.values())
        return jsonify({"status": "ok", "cameras_online": online, "cameras_total": len(workers),
                        "uptime_s": round(time.time() - started_at)})

    if https_port:
        # Quiet: werkzeug would otherwise log every pushed frame (10+ lines per second).
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        start_https_server(app, https_port, os.path.dirname(db_path) or "data")
    return app


if __name__ == "__main__":
    app = create_app()
    # 0.0.0.0 = accept connections from other machines too (needed in Docker / on Azure).
    # threaded=True: every browser connection (each video stream) gets its own thread.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), threaded=True)
