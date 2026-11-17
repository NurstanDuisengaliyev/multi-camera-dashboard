"""Dashboard pages and data.

  /                      dashboard page (HTML; cameras are drawn by static/dashboard.js)
  /video/<cam_id>        live MJPEG stream of one camera (used by <img> tags)
  /api/cameras           JSON: stats of all cameras + summary (also tells the page which cameras exist)
  /api/history           JSON: last 5 minutes of people/motion per camera (charts)
  /api/events?limit=50   JSON: latest alerts from the SQLite event log
  /health                JSON: is the service alive? (Docker health check, no password)
"""
import time

import cv2
import numpy as np
from flask import Blueprint, Response, abort, current_app, jsonify, render_template, request

bp = Blueprint("dashboard", __name__)


def placeholder_jpeg(title, text, color):
    """Dark image shown instead of video, e.g. 'NO SIGNAL' when a camera is offline."""
    img = np.full((360, 640, 3), 30, np.uint8)
    (w, _), _ = cv2.getTextSize(title, cv2.FONT_HERSHEY_SIMPLEX, 1.2, 3)
    cv2.putText(img, title, ((640 - w) // 2, 170), cv2.FONT_HERSHEY_SIMPLEX, 1.2, color, 3, cv2.LINE_AA)
    cv2.putText(img, text, (20, 220), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1, cv2.LINE_AA)
    return cv2.imencode(".jpg", img)[1].tobytes()


def mjpeg_stream(worker, fps, quality):
    """Generator: yields one JPEG 'part' per new frame. The browser replaces the picture
    with each part, which looks like video. Ends when the camera is removed."""
    interval = 1.0 / fps
    label = "%s (%s)" % (worker.cam_name, worker.cam_id)
    if worker.is_phone:
        placeholder = placeholder_jpeg("WAITING FOR PHONE", label, (180, 180, 180))
    else:
        placeholder = placeholder_jpeg("NO SIGNAL", label, (60, 60, 210))
    last_sent = -1
    while not worker.stopped:
        if worker.get_stats()["status"] == "online":
            jpeg, frame_no = worker.get_jpeg(quality)
        else:
            jpeg, frame_no = placeholder, -1
        if jpeg is not None and (frame_no != last_sent or frame_no == -1):
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
            last_sent = frame_no
        # Limit the stream FPS: saves bandwidth (cloud traffic costs money) and CPU.
        time.sleep(interval if frame_no != -1 else 1.0)


@bp.route("/")
def index():
    s = current_app.config["SETTINGS"]
    # A logged-in user gets a ready-made phone link with the token in it. The token is
    # after '#', so it stays in the browser and never shows up in server logs.
    phone_link = "/phone"
    if s["phone_token"] and s["dashboard_password"]:
        phone_link += "#token=" + s["phone_token"]
    return render_template("dashboard.html", phone_link=phone_link)


@bp.route("/video/<cam_id>")
def video(cam_id):
    s = current_app.config["SETTINGS"]
    worker = current_app.config["CAMERAS"].get(cam_id) or abort(404)
    return Response(mjpeg_stream(worker, s["stream_fps"], s["jpeg_quality"]),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@bp.route("/api/cameras")
def api_cameras():
    cams = [w.get_stats() for w in current_app.config["CAMERAS"].all()]
    summary = {
        "total": len(cams),
        "online": sum(c["status"] == "online" for c in cams),
        "people": sum(c["people"] for c in cams if c["status"] == "online"),
        "active_alerts": sum(len(c["alerts"]) for c in cams),
    }
    return jsonify({"cameras": cams, "summary": summary, "server_time": time.time()})


@bp.route("/api/history")
def api_history():
    return jsonify({w.cam_id: w.get_history() for w in current_app.config["CAMERAS"].all()})


@bp.route("/api/events")
def api_events():
    limit = min(request.args.get("limit", 50, type=int), 500)
    return jsonify(current_app.config["EVENTS"].recent(limit))


@bp.route("/health")
def health():
    cams = [w.get_stats() for w in current_app.config["CAMERAS"].all()]
    return jsonify({"status": "ok",
                    "cameras_online": sum(c["status"] == "online" for c in cams),
                    "cameras_total": len(cams),
                    "uptime_s": round(time.time() - current_app.config["STARTED_AT"])})
