"""CameraManager: the list of all cameras and their worker threads.

Two kinds of camera:
  - video file cameras, listed in config.yaml, running from start to end of the program;
  - phone cameras, created when a phone starts streaming on the /phone page and
    removed when the phone taps "Stop" or has been silent for `remove_after_s`.

The web server asks the manager for cameras; it never creates workers itself.
"""
import logging
import re
import secrets
import threading
import time

from app.cameras.sources import FrameInbox, PhoneSource
from app.cameras.worker import CameraWorker

log = logging.getLogger("cameras")

PHONE_ID = re.compile(r"^phone-[0-9a-f]{6}$")
CLEANUP_EVERY_S = 5


class TooManyPhones(Exception):
    pass


class CameraManager:
    def __init__(self, config, detector=None, events=None):
        self.analytics = config["analytics"]
        self.file_cameras = config.get("cameras", [])
        phones = config.get("phones", {})
        self.max_phones = phones.get("max_cameras", 4)
        self.phone_crowd_limit = phones.get("crowd_limit", 3)
        self.remove_after_s = phones.get("remove_after_s", 60)

        self.detector = detector
        self.events = events
        self.inbox = FrameInbox()  # phone uploads land here, phone workers read from here
        self.workers = {}          # cam_id -> CameraWorker (in the order they were added)
        self.lock = threading.Lock()
        self.newest_seq = {}       # cam_id -> capture time of the newest phone frame received
        self.seq_lock = threading.Lock()

    def start(self):
        for cam in self.file_cameras:
            self._start_worker(CameraWorker(cam, self.analytics, self.detector, self.events))
        threading.Thread(target=self._remove_silent_phones, name="phone-cleanup", daemon=True).start()
        log.info("started %d video cameras: %s", len(self.workers), ", ".join(self.workers))

    # ---------- read access (used by the web server) ----------
    def all(self):
        with self.lock:
            return list(self.workers.values())

    def get(self, cam_id):
        with self.lock:
            return self.workers.get(cam_id)

    def get_phone(self, cam_id):
        worker = self.get(cam_id)
        return worker if worker is not None and worker.is_phone else None

    # ---------- phone cameras ----------
    def add_phone(self, name, wanted_id=None, device="Phone"):
        """Create a camera for a phone and return its worker.
        A phone that streamed before sends its old id back, so it keeps the same camera
        (same color on the charts, same name in the event log)."""
        with self.lock:
            existing = self.workers.get(wanted_id)
            if existing is not None and existing.is_phone:
                existing.cam_name = name
                existing.device = device
                return existing
            if sum(w.is_phone for w in self.workers.values()) >= self.max_phones:
                raise TooManyPhones("At most %d phones can stream at the same time" % self.max_phones)
            cam_id = wanted_id if wanted_id and PHONE_ID.match(wanted_id) else self._new_phone_id()
            cam = {"id": cam_id, "name": name, "crowd_limit": self.phone_crowd_limit}
            timeout_s = self.analytics.get("offline_after_s", 5)
            worker = CameraWorker(cam, self.analytics, self.detector, self.events,
                                  new_source=lambda: PhoneSource(cam_id, self.inbox, timeout_s))
            worker.device = device
            self.workers[cam_id] = worker  # registered inside the lock: no duplicate ids
        worker.start()
        log.info("phone camera %s (%s) added", cam_id, name)
        return worker

    def remove_phone(self, cam_id):
        with self.lock:
            worker = self.workers.get(cam_id)
            if worker is None or not worker.is_phone:
                return False
            del self.workers[cam_id]
        worker.stop()
        self.inbox.forget(cam_id)
        log.info("phone camera %s (%s) removed", cam_id, worker.cam_name)
        return True

    def phone_count(self):
        return sum(w.is_phone for w in self.all())

    def receive_frame(self, cam_id, jpeg, rtt_ms=None, seq=None):
        # The phone uploads several frames at once, so an older one can arrive after a newer
        # one. seq = capture time on the phone: drop late frames so video never goes backwards.
        if seq is not None:
            with self.seq_lock:
                if seq <= self.newest_seq.get(cam_id, -1):
                    return
                self.newest_seq[cam_id] = seq
        self.inbox.put(cam_id, jpeg)
        worker = self.get(cam_id)
        if worker is not None and rtt_ms is not None:
            worker.phone_rtt_ms = rtt_ms

    # ---------- internals ----------
    def _start_worker(self, worker):
        with self.lock:
            self.workers[worker.cam_id] = worker
        worker.start()

    def _new_phone_id(self):  # called with self.lock held
        while True:
            cam_id = "phone-" + secrets.token_hex(3)
            if cam_id not in self.workers:
                return cam_id

    def _remove_silent_phones(self):
        """Background thread: remove phone cameras that stopped sending a while ago."""
        while True:
            time.sleep(CLEANUP_EVERY_S)
            now = time.monotonic()
            for worker in self.all():
                if not worker.is_phone:
                    continue
                last = self.inbox.last_received(worker.cam_id) or worker.created_at
                if now - last > self.remove_after_s:
                    self.remove_phone(worker.cam_id)
