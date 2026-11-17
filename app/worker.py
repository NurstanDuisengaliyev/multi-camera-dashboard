"""CameraWorker: one background thread per camera.

Loop:  read frame -> resize -> analytics (motion, quality, people) -> alerts
       -> draw boxes -> store latest frame + stats + history.
Other parts of the program (Flask) only READ the latest frame, stats and history.
"""
import logging
import threading
import time
from collections import deque

import cv2

from app.analytics.motion import MotionDetector
from app.analytics.quality import measure
from app.sources import make_source

RECONNECT_DELAY_S = 2
HISTORY_SECONDS = 300  # keep 5 minutes of one-per-second samples for the charts

GREEN = (80, 200, 60)
YELLOW = (0, 215, 255)
RED = (40, 40, 230)
WHITE = (255, 255, 255)


class HeldCondition:
    """Becomes True only after a condition has been true for `hold_s` seconds in a row.
    This stops one noisy frame from triggering an alert."""

    def __init__(self, hold_s):
        self.hold_s = hold_s
        self.since = None

    def update(self, active, now):
        if not active:
            self.since = None
            return False
        if self.since is None:
            self.since = now
        return now - self.since >= self.hold_s


class CameraWorker(threading.Thread):
    def __init__(self, cam_config, analytics_config, detector=None, events=None):
        # daemon=True: the thread stops automatically when the main program exits.
        super().__init__(name=cam_config["id"], daemon=True)
        a = analytics_config
        self.cam_id = cam_config["id"]
        self.cam_name = cam_config.get("name", self.cam_id)
        self.source_spec = cam_config["source"]
        self.crowd_limit = cam_config.get("crowd_limit", 5)

        self.resize_width = a.get("resize_width", 640)
        self.offline_after_s = a.get("offline_after_s", 5)
        self.motion_threshold = a.get("motion_threshold", 0.02)
        self.detect_interval_s = a.get("detect_interval_s", 0.5)
        self.dark_threshold = a.get("dark_threshold", 40)
        self.blur_threshold = a.get("blur_threshold", 8)
        hold = a.get("alert_hold_s", 1.5)

        self.detector = detector  # shared PeopleDetector (or None = no people analytics)
        self.events = events      # shared EventStore (or None = no event log)
        self.motion = MotionDetector(min_area=a.get("motion_min_area", 400))
        self.conditions = {name: HeldCondition(hold) for name in ("motion", "crowd", "dark", "blurry")}

        self.log = logging.getLogger(self.cam_id)
        self.lock = threading.Lock()   # protects the shared state below
        self.stop_event = threading.Event()

        # --- state shared with other threads (read with get_* methods) ---
        self.latest_frame = None
        self.last_frame_time = 0.0
        self.frames_total = 0
        self.frame_times = deque()     # timestamps of frames in the last second (for FPS)
        self.history = deque(maxlen=HISTORY_SECONDS)
        self.analysis = {"people": 0, "motion_pct": 0.0, "brightness": 0.0,
                         "sharpness": 0.0, "alerts": []}
        self.timing_ms = {"motion": 0.0, "quality": 0.0, "people": 0.0}

        # --- state used only inside this thread ---
        self.people_boxes = []
        self.last_detect = 0.0
        self.last_history = 0.0
        self.online = None             # None = not known yet, True/False after that

    # ================= main loop (runs inside the thread) =================
    def run(self):
        self.log.info("worker started, source=%s", self.source_spec)
        while not self.stop_event.is_set():
            source = make_source(self.source_spec)
            if not source.open():
                self._set_online(False, "cannot open source")
                self.stop_event.wait(RECONNECT_DELAY_S)
                continue

            self.log.info("source opened (%.1f fps)", source.fps)
            while not self.stop_event.is_set():
                frame = source.read()
                if frame is None:
                    self._set_online(False, "source stopped sending frames")
                    break
                self._set_online(True, "receiving frames")
                self._process(frame)

            source.close()
            self.stop_event.wait(RECONNECT_DELAY_S)
        self.log.info("worker stopped")

    def _process(self, frame):
        frame = self._resize(frame)
        now = time.monotonic()

        # 1) Motion: background subtraction, every frame (cheap).
        t = time.perf_counter()
        motion_ratio, motion_boxes = self.motion.update(frame)
        self._timing("motion", t)

        # 2) Image quality for tampering: brightness and sharpness (cheap).
        t = time.perf_counter()
        brightness, sharpness = measure(frame)
        self._timing("quality", t)

        # 3) People: YOLO is expensive, so only every `detect_interval_s` seconds.
        #    Between detections we keep showing the last boxes.
        if self.detector is not None and now - self.last_detect >= self.detect_interval_s:
            t = time.perf_counter()
            self.people_boxes = self.detector.detect(frame)
            self._timing("people", t)
            self.last_detect = now
        people = len(self.people_boxes)

        # 4) Alerts: each condition must hold for a moment before it fires.
        is_dark = brightness < self.dark_threshold
        active = {
            "motion": motion_ratio > self.motion_threshold,
            "crowd": people > self.crowd_limit,
            "dark": is_dark,
            # A dark image has no edges either, so only call it "blurry" when it is NOT dark.
            "blurry": sharpness < self.blur_threshold and not is_dark,
        }
        alerts = [name for name, is_on in active.items() if self.conditions[name].update(is_on, now)]
        self._send_alerts(alerts, people, motion_ratio, brightness, sharpness)

        annotated = self._draw(frame, motion_boxes, people, motion_ratio, alerts)

        # 5) Publish results for other threads.
        self.frame_times.append(now)
        while self.frame_times and now - self.frame_times[0] > 1.0:
            self.frame_times.popleft()
        with self.lock:
            self.latest_frame = annotated
            self.last_frame_time = now
            self.frames_total += 1
            self.analysis = {"people": people, "motion_pct": round(motion_ratio * 100, 1),
                             "brightness": round(brightness), "sharpness": round(sharpness),
                             "alerts": alerts}
            if now - self.last_history >= 1.0:  # one chart sample per second
                self.history.append({"t": round(time.time()), "people": people,
                                     "motion_pct": round(motion_ratio * 100, 1)})
                self.last_history = now

    def _send_alerts(self, alerts, people, motion_ratio, brightness, sharpness):
        if self.events is None:
            return
        messages = {
            "motion": ("motion", "Motion detected (%.1f%% of image)" % (motion_ratio * 100), motion_ratio * 100),
            "crowd": ("crowd", "Crowd: %d people (limit %d)" % (people, self.crowd_limit), people),
            "dark": ("camera_dark", "Image too dark (brightness %.0f), camera covered?" % brightness, brightness),
            "blurry": ("camera_blurry", "Image blurry (sharpness %.1f), lens dirty?" % sharpness, sharpness),
        }
        for name in alerts:
            event_type, message, value = messages[name]
            self.events.add(self.cam_id, self.cam_name, event_type, message, round(value, 1))

    def _set_online(self, online, reason):
        if online == self.online:
            return
        was_known = self.online is not None
        self.online = online
        if online:
            self.log.info("camera online (%s)", reason)
            if was_known and self.events:  # only report "back online" after an outage
                self.events.add(self.cam_id, self.cam_name, "camera_online", "Camera back online", cooldown=False)
        else:
            self.log.warning("camera offline (%s), retrying every %ss", reason, RECONNECT_DELAY_S)
            if self.events:
                self.events.add(self.cam_id, self.cam_name, "camera_offline",
                                "Camera offline: " + reason, cooldown=False)

    # ================= helpers =================
    def _resize(self, frame):
        h, w = frame.shape[:2]
        if w <= self.resize_width:
            return frame
        return cv2.resize(frame, (self.resize_width, int(h * self.resize_width / w)))

    def _timing(self, name, start):
        # Smoothed average (exponential moving average) so the number doesn't jump around.
        ms = (time.perf_counter() - start) * 1000
        self.timing_ms[name] = round(0.9 * self.timing_ms[name] + 0.1 * ms, 1) if self.timing_ms[name] else round(ms, 1)

    def _draw(self, frame, motion_boxes, people, motion_ratio, alerts):
        img = frame.copy()
        for (x, y, w, h) in motion_boxes:
            cv2.rectangle(img, (x, y), (x + w, y + h), YELLOW, 1)
        for (x1, y1, x2, y2, conf) in self.people_boxes:
            cv2.rectangle(img, (x1, y1), (x2, y2), GREEN, 2)
            cv2.putText(img, "person %.2f" % conf, (x1, max(12, y1 - 5)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, GREEN, 1, cv2.LINE_AA)

        # Info bar at the top.
        cv2.rectangle(img, (0, 0), (img.shape[1], 24), (0, 0, 0), -1)
        text = "%s | people: %d | motion: %.1f%%" % (self.cam_name, people, motion_ratio * 100)
        cv2.putText(img, text, (6, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.5, WHITE, 1, cv2.LINE_AA)
        if alerts:
            label = " ".join(a.upper() for a in alerts)
            (tw, _), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.putText(img, label, (img.shape[1] - tw - 6, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.5, RED, 1, cv2.LINE_AA)
        return img

    # ================= used by other threads =================
    def stop(self):
        self.stop_event.set()

    def get_frame(self):
        with self.lock:
            return None if self.latest_frame is None else self.latest_frame.copy()

    def get_history(self):
        with self.lock:
            return list(self.history)

    def get_stats(self):
        with self.lock:
            age = time.monotonic() - self.last_frame_time if self.last_frame_time else None
            online = age is not None and age < self.offline_after_s
            stats = {
                "id": self.cam_id,
                "name": self.cam_name,
                "status": "online" if online else "offline",
                "fps": len(self.frame_times),
                "frames_total": self.frames_total,
                "last_frame_age_s": None if age is None else round(age, 2),
                "crowd_limit": self.crowd_limit,
                "timing_ms": dict(self.timing_ms),
            }
            stats.update(self.analysis)
            if not online:
                stats["alerts"] = []
            return stats
