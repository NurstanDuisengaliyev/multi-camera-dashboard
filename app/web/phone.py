"""Phone streaming: any phone can become a camera.

  /phone                         page that opens the phone camera and sends its video (static/phone.js)
  POST /api/phone/start          {"name": "Front door", "camera_id": optional old id,
                                  "transport": "webrtc" | "jpeg"} -> {"camera_id": ..., "transport": ...}
  POST /api/phone/<id>/frame     JPEG fallback: body = one JPEG image (+ headers X-Phone-RTT: last
                                 round trip in ms, X-Phone-Seq: capture time, so late frames are dropped)
  POST /api/phone/<id>/stats     WebRTC: {"rtt_ms": ...} every few seconds (shown on the dashboard)
  POST /api/phone/<id>/stop      the phone tapped "Stop": remove its camera

With WebRTC the video itself does not go through Flask: the phone sends it to MediaMTX
(POST /webrtc/<id>-in/whip, allowed by webrtc.py) and the camera worker reads it from there.

Every API call needs the header  X-Phone-Token: <PHONE_TOKEN>.
Without PHONE_TOKEN set on the server, phone streaming is switched off.
The phone page must be opened over HTTPS: phones only allow the camera on secure pages.
"""
from flask import Blueprint, current_app, jsonify, render_template, request

from app.cameras.manager import TooManyPhones
from app.web.auth import same

bp = Blueprint("phone", __name__)

MAX_NAME_LENGTH = 40


def device_name():
    """A short label for the dashboard card, from the browser's User-Agent."""
    ua = request.headers.get("User-Agent", "")
    for marker, label in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android")):
        if marker in ua:
            return label
    return "Phone"


def check_token():
    """Returns an error response, or None if the phone sent the right token."""
    token = current_app.config["SETTINGS"]["phone_token"]
    if not token:
        return jsonify({"error": "Phone streaming is off: set PHONE_TOKEN on the server"}), 503
    if not same(request.headers.get("X-Phone-Token", ""), token):
        return jsonify({"error": "Wrong phone token"}), 403
    return None


@bp.route("/phone")
def page():
    s = current_app.config["SETTINGS"]
    return render_template("phone.html", enabled=bool(s["phone_token"]), webrtc=bool(s["webrtc"]))


@bp.route("/api/phone/start", methods=["POST"])
def start():
    error = check_token()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    name = " ".join(str(data.get("name", "")).split())[:MAX_NAME_LENGTH] or "Phone"
    try:
        worker = current_app.config["CAMERAS"].add_phone(name, data.get("camera_id"), device_name(),
                                                         data.get("transport", "jpeg"))
    except TooManyPhones as e:
        return jsonify({"error": str(e)}), 429
    return jsonify({"camera_id": worker.cam_id, "name": worker.cam_name, "transport": worker.transport})


@bp.route("/api/phone/<cam_id>/frame", methods=["POST"])
def frame(cam_id):
    error = check_token()
    if error:
        return error
    cameras = current_app.config["CAMERAS"]
    if cameras.get_phone(cam_id) is None:
        # e.g. removed after the phone was silent too long: the page calls /start again
        return jsonify({"error": "Unknown phone camera"}), 404
    jpeg = request.get_data()
    if not jpeg.startswith(b"\xff\xd8"):  # every JPEG file starts with these two bytes
        return jsonify({"error": "Body must be a JPEG image"}), 400
    # The phone reports how long its previous upload took (shown as connection quality).
    rtt = request.headers.get("X-Phone-RTT", type=int)
    seq = request.headers.get("X-Phone-Seq", type=int)  # capture time, to drop late frames
    cameras.receive_frame(cam_id, jpeg, rtt, seq)
    return jsonify({"ok": True})


@bp.route("/api/phone/<cam_id>/stats", methods=["POST"])
def stats(cam_id):
    error = check_token()
    if error:
        return error
    worker = current_app.config["CAMERAS"].get_phone(cam_id)
    if worker is None:
        return jsonify({"error": "Unknown phone camera"}), 404  # the page registers again
    rtt = (request.get_json(silent=True) or {}).get("rtt_ms")
    if isinstance(rtt, (int, float)):
        worker.phone_rtt_ms = int(rtt)
    return jsonify({"ok": True})


@bp.route("/api/phone/<cam_id>/stop", methods=["POST"])
def stop(cam_id):
    error = check_token()
    if error:
        return error
    current_app.config["CAMERAS"].remove_phone(cam_id)
    return jsonify({"ok": True})
