"""Password protection for the dashboard (HTTP Basic auth).

The browser shows its own login box and then sends the user name + password with
every request, including the <img> video streams, so no login page or cookies are needed.
Use it only behind HTTPS (Caddy, see deploy/Caddyfile): over plain HTTP the password
would travel unencrypted.

Not protected:
  - /health          for the Docker health check
  - /static/...      CSS and JavaScript, nothing secret
  - /phone, /api/phone/...   phones prove themselves with PHONE_TOKEN instead (see phone.py),
                     so you can let someone stream without giving them the dashboard password.
  - /api/webrtc-auth  asked by Caddy before WebRTC requests; it checks the login itself (webrtc.py)
"""
import hmac
import logging

from flask import Response, current_app, request

log = logging.getLogger("auth")

PUBLIC_ENDPOINTS = {"dashboard.health", "static"}
PUBLIC_BLUEPRINTS = {"phone", "webrtc"}


def same(a, b):
    # compare_digest takes the same time whatever the input: an attacker can't guess
    # the password letter by letter by measuring how fast we answer.
    return hmac.compare_digest(a.encode(), b.encode())


def logged_in():
    """Did this request bring the right dashboard user name and password?"""
    s = current_app.config["SETTINGS"]
    if not s["dashboard_password"]:
        return True  # no password set: dashboard open (local use)
    given = request.authorization
    return bool(given) and same(given.username or "", s["dashboard_user"]) \
        and same(given.password or "", s["dashboard_password"])


def protect_with_password(app, user, password):
    if not password:
        log.warning("DASHBOARD_PASSWORD is not set: the dashboard is open to everyone")
        return

    @app.before_request
    def check_password():
        if request.endpoint in PUBLIC_ENDPOINTS or request.blueprint in PUBLIC_BLUEPRINTS:
            return None
        if logged_in():
            return None
        return Response("Login required", 401,
                        {"WWW-Authenticate": 'Basic realm="Camera dashboard", charset="UTF-8"'})
