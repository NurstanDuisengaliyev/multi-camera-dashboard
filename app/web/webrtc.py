"""Who may use WebRTC? Asked by Caddy before every WebRTC request.

The live video runs through MediaMTX (a WebRTC media server, see docker-compose.yml):
    dashboard: POST /webrtc/<cam_id>/whep          "send me this camera's video"   (WHEP standard)
    phone:     POST /webrtc/<phone_id>-in/whip     "here is my camera's video"     (WHIP standard)
The request body is an SDP "offer" (codecs, network addresses); MediaMTX answers with its own,
then the video flows directly over UDP port 8189 (TCP 8189 if UDP is blocked).

MediaMTX has no idea about our passwords, so Caddy first sends the request's headers here
(forward_auth, see deploy/Caddyfile) and only forwards it to MediaMTX if we answer 200:
    whep (watch)  needs the dashboard login, and the camera must exist
    whip (send)   needs the phone token, and only to the "-in" stream of a registered phone
"""
import re
from urllib.parse import urlsplit

from flask import Blueprint, current_app, request

from app.web.auth import logged_in, same

bp = Blueprint("webrtc", __name__)

PHONE_INPUT = re.compile(r"^(phone-[0-9a-f]{6})-in$")


@bp.route("/api/webrtc-auth")
def check():
    # Caddy passes the original address in X-Forwarded-Uri, e.g. /cam1/whep or /webrtc/cam1/whep
    # (also /cam1/whep/<session> for follow-up requests of the same connection).
    parts = urlsplit(request.headers.get("X-Forwarded-Uri", "")).path.strip("/").split("/")
    if parts[:1] == ["webrtc"]:
        parts = parts[1:]
    if len(parts) < 2:
        return "", 403
    stream, action = parts[0], parts[1]
    cameras = current_app.config["CAMERAS"]

    if action == "whep":  # a dashboard wants to watch
        if not logged_in():
            return "", 401
        return ("", 200) if cameras.get(stream) is not None else ("", 404)

    if action == "whip":  # a phone wants to send its camera
        token = current_app.config["SETTINGS"]["phone_token"]
        match = PHONE_INPUT.match(stream)
        if not token or not same(request.headers.get("X-Phone-Token", ""), token):
            return "", 403
        if match is None or cameras.get_phone(match.group(1)) is None:
            return "", 404  # register first: POST /api/phone/start
        return "", 200

    return "", 403
