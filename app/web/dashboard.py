"""Dashboard pages and data.

  /                              dashboard page (HTML; cameras are drawn by static/dashboard.js)
  /frame/<cam_id>?viewer=V       the next frame (JPEG) this viewer hasn't got yet: dashboard.js
                                 asks for frame after frame, which gives the live video
  /video/<cam_id>                live MJPEG stream of one camera (for opening a camera directly)
  /snapshot/<cam_id>.jpg         the current picture of one camera (?download=1: as a file)
  /api/cameras                   JSON: stats of all cameras + summary (tells the page which cameras exist)
  /api/history                   JSON: last 5 minutes of people/motion per camera (charts)
  /api/events?limit=50           JSON: latest alerts from the SQLite event log
  /api/events/<id>/snapshot.jpg  picture saved when that alert fired
  /api/events.csv                the whole event log as a CSV download
  /api/phone-link                JSON: link + QR code for adding a phone (contains the phone token)
  /health                        JSON: is the service alive? (Docker health check, no password)
"""
import io
import threading
import time
from functools import lru_cache

import cv2
import numpy as np
import qrcode
import qrcode.image.svg
from flask import (Blueprint, Response, abort, current_app, jsonify, render_template, request,
                   send_file)

bp = Blueprint("dashboard", __name__)

# Which frame each open video tile got last: (viewer id, camera worker) -> frame number.
# A tile keeps a few requests open at once (see playStream() in dashboard.js); this makes
# sure each request gets a DIFFERENT, newer frame instead of all of them the same one.
sent_to_viewer = {}
sent_lock = threading.Lock()


@lru_cache(maxsize=64)  # same arguments -> same picture: draw it once, not on every request
def placeholder_jpeg(title, text, color):
    """Dark image shown instead of video, e.g. 'NO SIGNAL' when a camera is offline."""
    img = np.full((360, 640, 3), 30, np.uint8)
    (w, _), _ = cv2.getTextSize(title, cv2.FONT_HERSHEY_SIMPLEX, 1.2, 3)
    cv2.putText(img, title, ((640 - w) // 2, 170), cv2.FONT_HERSHEY_SIMPLEX, 1.2, color, 3, cv2.LINE_AA)
    cv2.putText(img, text, (20, 220), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1, cv2.LINE_AA)
    return cv2.imencode(".jpg", img)[1].tobytes()


def placeholder_for(worker):
    label = "%s (%s)" % (worker.cam_name, worker.cam_id)
    if worker.is_phone:
        return placeholder_jpeg("WAITING FOR PHONE", label, (180, 180, 180))
    return placeholder_jpeg("NO SIGNAL", label, (60, 60, 210))


def mjpeg_stream(worker, fps, quality):
    """Generator: yields one JPEG 'part' per new frame. The browser replaces the picture
    with each part, which looks like video. Ends when the camera is removed."""
    interval = 1.0 / fps
    placeholder = placeholder_for(worker)
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


def phone_link():
    """Address of the phone page with the token after '#'. The part after '#' stays in the
    phone's browser and never reaches server logs. None when phone streaming is off."""
    s = current_app.config["SETTINGS"]
    if not s["phone_token"]:
        return None
    if not s["dashboard_password"]:
        # Dashboard open to everyone: don't show the token to everyone too.
        return request.host_url + "phone"
    return request.host_url + "phone#token=" + s["phone_token"]


@bp.route("/")
def index():
    return render_template("dashboard.html", webrtc=bool(current_app.config["SETTINGS"]["webrtc"]))


@bp.route("/frame/<cam_id>")
def frame(cam_id):
    """The live video of the dashboard, one frame per request.
    Why not a pushed stream (/video)? It keeps sending even when the viewer's connection is
    slower than the video: frames pile up in network buffers and the picture falls seconds
    behind. Here the browser has only a few requests open per camera (dashboard.js), so at
    most that many frames are on the way: a slow connection lowers the FPS, never adds delay.
    Each request waits (max 2 s) for a frame this viewer hasn't received yet."""
    s = current_app.config["SETTINGS"]
    worker = current_app.config["CAMERAS"].get(cam_id) or abort(404)
    if worker.get_stats()["status"] != "online":
        jpeg, frame_no = placeholder_for(worker), 0  # 0 = "placeholder, ask again in a second"
    else:
        key = (request.args.get("viewer", ""), id(worker))
        deadline = time.monotonic() + 2.0
        while True:
            with sent_lock:
                after = sent_to_viewer.get(key, 0)
            jpeg, frame_no = worker.wait_jpeg(after, deadline - time.monotonic(), s["jpeg_quality"])
            if jpeg is None or frame_no <= after:
                return "", 204  # nothing new within 2 s: the browser simply asks again
            with sent_lock:
                if frame_no > sent_to_viewer.get(key, 0):  # not taken by a parallel request
                    if len(sent_to_viewer) > 2000:
                        sent_to_viewer.clear()  # old viewers (closed tabs): just start over
                    sent_to_viewer[key] = frame_no
                    break
    return Response(jpeg, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store", "X-Frame-No": str(frame_no),
                             "X-Stream-Fps": str(s["stream_fps"])})


@bp.route("/video/<cam_id>")
def video(cam_id):
    s = current_app.config["SETTINGS"]
    worker = current_app.config["CAMERAS"].get(cam_id) or abort(404)
    return Response(mjpeg_stream(worker, s["stream_fps"], s["jpeg_quality"]),
                    mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store"})


@bp.route("/snapshot/<cam_id>.jpg")
def snapshot(cam_id):
    worker = current_app.config["CAMERAS"].get(cam_id) or abort(404)
    jpeg, _ = worker.get_jpeg(90)
    if jpeg is None:
        abort(404)
    # ?download=1 -> "Save snapshot" button; without it -> small previews on the page.
    name = "%s_%s.jpg" % (cam_id, time.strftime("%Y%m%d_%H%M%S"))
    return send_file(io.BytesIO(jpeg), mimetype="image/jpeg", max_age=0,
                     as_attachment=request.args.get("download") == "1", download_name=name)


@bp.route("/api/cameras")
def api_cameras():
    cameras = current_app.config["CAMERAS"]
    cams = [w.get_stats() for w in cameras.all()]
    summary = {
        "total": len(cams),
        "online": sum(c["status"] == "online" for c in cams),
        "people": sum(c["people"] for c in cams if c["status"] == "online"),
        "active_alerts": sum(len(c["alerts"]) for c in cams) + sum(c["status"] == "offline" for c in cams),
        "phones": cameras.phone_count(),
        "max_phones": cameras.max_phones,
        "phone_remove_after_s": cameras.remove_after_s,
    }
    return jsonify({"cameras": cams, "summary": summary, "server_time": time.time()})


@bp.route("/api/history")
def api_history():
    return jsonify({w.cam_id: w.get_history() for w in current_app.config["CAMERAS"].all()})


@bp.route("/api/events")
def api_events():
    limit = min(request.args.get("limit", 50, type=int), 500)
    return jsonify(current_app.config["EVENTS"].recent(limit))


@bp.route("/api/events/<int:event_id>/snapshot.jpg")
def event_snapshot(event_id):
    path = current_app.config["EVENTS"].snapshot_path(event_id)
    try:
        return send_file(path, mimetype="image/jpeg", max_age=86400)
    except FileNotFoundError:
        abort(404)


@bp.route("/api/events.csv")
def events_csv():
    name = "events_%s.csv" % time.strftime("%Y%m%d_%H%M%S")
    return Response(current_app.config["EVENTS"].csv_text(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=" + name})


@bp.route("/api/phone-link")
def api_phone_link():
    link = phone_link()
    if link is None:
        return jsonify({"enabled": False})
    svg = io.BytesIO()
    qrcode.make(link, image_factory=qrcode.image.svg.SvgPathImage, border=1).save(svg)
    return jsonify({"enabled": True, "link": link, "has_token": "#token=" in link,
                    "qr_svg": svg.getvalue().decode()})


@bp.route("/health")
def health():
    cams = [w.get_stats() for w in current_app.config["CAMERAS"].all()]
    return jsonify({"status": "ok",
                    "cameras_online": sum(c["status"] == "online" for c in cams),
                    "cameras_total": len(cams),
                    "uptime_s": round(time.time() - current_app.config["STARTED_AT"])})
