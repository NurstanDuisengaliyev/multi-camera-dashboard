"""Entry point: start the camera workers and the web server.

    config.yaml + environment variables
            |
    CameraManager (app/cameras)  -- one CameraWorker thread per camera, runs the analytics
            |
    Flask web server (app/web)   -- dashboard pages, JSON API, video streams, phone uploads

Run locally:   python -m app.main                  (http://localhost:8000)
In Docker:     gunicorn "app.main:create_app()"    (see Dockerfile; Caddy adds HTTPS in front)

Environment variables:
  DASHBOARD_USER, DASHBOARD_PASSWORD   login for the dashboard (no password = open, local use only)
  PHONE_TOKEN                          secret phones must send to stream (not set = phones off)
  MEDIAMTX_RTSP                        e.g. rtsp://mediamtx:8554: live video over WebRTC through
                                       MediaMTX (not set = JPEG frames only, e.g. local run)
  CONFIG_PATH, LOG_DIR, DATABASE, PORT
"""
import os
import time

import cv2
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from app.analytics.people import PeopleDetector
from app.cameras.manager import CameraManager
from app.config import load_config, setup_logging
from app.events import EventStore
from app.web import auth, dashboard, phone, webrtc

MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # one phone frame is ~20-300 KB; refuse anything huge


def create_app():
    # Each camera already has its own thread. If OpenCV ALSO splits every operation over all
    # cores, its helper threads mostly wait for each other on a 2-vCPU VM (measured: ~0.25
    # cores wasted with 4 cameras). One OpenCV thread per camera thread is enough.
    cv2.setNumThreads(1)
    setup_logging("app.log")
    config = load_config()
    a = config["analytics"]
    server = config.get("server", {})
    settings = {
        "stream_fps": server.get("stream_fps", 15),
        "jpeg_quality": server.get("jpeg_quality", 75),
        "dashboard_user": os.environ.get("DASHBOARD_USER", "admin"),
        "dashboard_password": os.environ.get("DASHBOARD_PASSWORD", ""),
        "phone_token": os.environ.get("PHONE_TOKEN", ""),
        "webrtc": os.environ.get("MEDIAMTX_RTSP", ""),
    }

    # 1) Shared services, then the cameras.
    events = EventStore(os.environ.get("DATABASE", config.get("database", "data/events.db")),
                        cooldown_s=a.get("alert_cooldown_s", 30))
    detector = PeopleDetector(a.get("model", "yolov8n.pt"), a.get("person_confidence", 0.4),
                              a.get("detect_imgsz", 640), a.get("detect_interval_s", 1.0))
    cameras = CameraManager(config, detector, events, rtsp_base=settings["webrtc"])
    cameras.start()
    events.add("system", "System", "system_start",
               "Monitoring started with %d cameras" % len(cameras.all()), cooldown=False)

    # 2) Web server. Routes find the services in app.config.
    app = Flask(__name__)
    app.config.update(SETTINGS=settings, CAMERAS=cameras, EVENTS=events,
                      STARTED_AT=time.time(), MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES)
    # Behind Caddy: trust its X-Forwarded-Proto/Host headers, so links the app builds
    # (the phone QR code) use https:// and the public name. Port 8000 is not public.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
    app.register_blueprint(dashboard.bp)
    app.register_blueprint(phone.bp)
    app.register_blueprint(webrtc.bp)
    auth.protect_with_password(app, settings["dashboard_user"], settings["dashboard_password"])
    return app


if __name__ == "__main__":
    app = create_app()
    # 0.0.0.0 = accept connections from other machines too (needed in Docker / on Azure).
    # threaded=True: every browser connection (each video stream) gets its own thread.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), threaded=True)
