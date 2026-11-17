"""CameraWorker: one background thread per camera.

Loop: read frame -> resize -> (analytics, added in step 2) -> store latest frame + stats.
Other parts of the program (later: Flask) only READ the latest frame and stats.
"""
import logging
import threading
import time
from collections import deque

import cv2

from app.sources import make_source

RECONNECT_DELAY_S = 2


class CameraWorker(threading.Thread):
    def __init__(self, cam_config, analytics_config):
        # daemon=True: the thread stops automatically when the main program exits.
        super().__init__(name=cam_config["id"], daemon=True)
        self.cam_id = cam_config["id"]
        self.cam_name = cam_config.get("name", self.cam_id)
        self.source_spec = cam_config["source"]
        self.resize_width = analytics_config.get("resize_width", 640)
        self.offline_after_s = analytics_config.get("offline_after_s", 5)

        self.log = logging.getLogger(self.cam_id)
        self.lock = threading.Lock()   # protects latest_frame / stats from being read mid-update
        self.stop_event = threading.Event()

        self.latest_frame = None
        self.last_frame_time = 0.0
        self.frame_times = deque()     # timestamps of frames in the last second (for FPS)
        self.frames_total = 0

    # ---------- main loop (runs inside the thread) ----------
    def run(self):
        self.log.info("worker started, source=%s", self.source_spec)
        while not self.stop_event.is_set():
            source = make_source(self.source_spec)
            if not source.open():
                self.log.warning("cannot open source, retrying in %ss", RECONNECT_DELAY_S)
                self.stop_event.wait(RECONNECT_DELAY_S)
                continue

            self.log.info("source opened (%.1f fps)", source.fps)
            while not self.stop_event.is_set():
                frame = source.read()
                if frame is None:
                    self.log.warning("source returned no frame, reconnecting")
                    break
                self._process(frame)

            source.close()
            self.stop_event.wait(RECONNECT_DELAY_S)
        self.log.info("worker stopped")

    def _process(self, frame):
        frame = self._resize(frame)
        now = time.monotonic()

        # FPS = how many frames we handled during the last 1 second.
        self.frame_times.append(now)
        while self.frame_times and now - self.frame_times[0] > 1.0:
            self.frame_times.popleft()

        with self.lock:
            self.latest_frame = frame
            self.last_frame_time = now
            self.frames_total += 1

    def _resize(self, frame):
        h, w = frame.shape[:2]
        if w <= self.resize_width:
            return frame
        new_h = int(h * self.resize_width / w)
        return cv2.resize(frame, (self.resize_width, new_h))

    # ---------- used by other threads ----------
    def stop(self):
        self.stop_event.set()

    def get_frame(self):
        with self.lock:
            return None if self.latest_frame is None else self.latest_frame.copy()

    def get_stats(self):
        with self.lock:
            age = time.monotonic() - self.last_frame_time if self.last_frame_time else None
            online = age is not None and age < self.offline_after_s
            return {
                "id": self.cam_id,
                "name": self.cam_name,
                "status": "online" if online else "offline",
                "fps": len(self.frame_times),
                "frames_total": self.frames_total,
                "last_frame_age_s": None if age is None else round(age, 2),
            }
