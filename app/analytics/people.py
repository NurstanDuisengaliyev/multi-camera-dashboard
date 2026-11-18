"""Person detection with YOLOv8n (ultralytics).

YOLO is a neural network pretrained on the COCO dataset (80 object types).
We only keep class 0 = "person".

One model is shared by all cameras, and ONE detection thread runs it:

    camera threads --request(newest frame)--> [pending: one frame per camera]
                                                     |
      detection thread, every interval_s: take ALL pending frames -> one batched YOLO call
                                                     |
                     each camera gets its boxes back through its callback

Why like this:
  - batching: analyzing 6 pictures in one call is ~1.35x cheaper than 6 separate calls
  - fair: every camera that asked is in the next batch, nobody waits for a lock
  - only the newest frame per camera is kept, so a busy server skips old frames
    instead of building up a queue (people counts stay "now")
  - camera threads never wait for YOLO, so the video stays smooth
"""
import logging
import threading
import time

from ultralytics import YOLO

PERSON_CLASS = 0
MAX_BATCH = 8

log = logging.getLogger("people")


class PeopleDetector:
    def __init__(self, model_path="yolov8n.pt", confidence=0.4, imgsz=640, interval_s=1.0):
        self.model = YOLO(model_path)  # downloads the 6 MB weights file the first time
        self.confidence = confidence
        self.imgsz = imgsz             # YOLO input size: smaller = faster, but small people get missed
        self.interval_s = interval_s   # one batch (= one detection per camera) this often
        self.lock = threading.Lock()   # one YOLO call at a time (detect() can still be called directly)
        self.pending = {}              # cam_id -> (frame, callback): the newest request per camera
        self.pending_lock = threading.Lock()
        self.wakeup = threading.Event()
        self.thread = None
        self.batches = 0               # statistics for the logs
        self.frames = 0

    # ---------- used by camera threads ----------
    def request(self, cam_id, frame, callback):
        """Ask for people detection on `frame`; returns immediately. Cameras call this for every
        frame they want analyzed; a newer frame replaces an older one that hasn't run yet.
        At the next batch, the detection thread calls callback(boxes, ms_per_frame)."""
        with self.pending_lock:
            self.pending[cam_id] = (frame, callback)
            if self.thread is None:
                self.thread = threading.Thread(target=self._loop, name="yolo", daemon=True)
                self.thread.start()
        self.wakeup.set()

    def cancel(self, cam_id):
        with self.pending_lock:
            self.pending.pop(cam_id, None)

    # ---------- detection ----------
    def detect(self, frame):
        """Boxes (x1, y1, x2, y2, confidence) of the people in one frame (synchronous)."""
        return self.detect_batch([frame])[0]

    def detect_batch(self, frames):
        """One YOLO call for several frames -> one list of boxes per frame."""
        with self.lock:
            results = self.model(frames, classes=[PERSON_CLASS], conf=self.confidence,
                                 imgsz=self.imgsz, verbose=False)
        out = []
        for r in results:
            boxes = []
            for box in r.boxes:
                x1, y1, x2, y2 = (int(v) for v in box.xyxy[0].tolist())
                boxes.append((x1, y1, x2, y2, float(box.conf[0])))
            out.append(boxes)
        return out

    def _loop(self):
        next_batch = time.monotonic()
        while True:
            self.wakeup.wait()
            # The detection thread sets the pace: one batch per interval. Waiting until then
            # lets every camera put its newest frame in, so they are analyzed together.
            time.sleep(max(0.0, next_batch - time.monotonic()))
            next_batch = max(next_batch + self.interval_s, time.monotonic())
            self.wakeup.clear()
            with self.pending_lock:
                jobs = list(self.pending.items())[:MAX_BATCH]
                for cam_id, _ in jobs:
                    del self.pending[cam_id]
            if self.pending:
                self.wakeup.set()  # more than MAX_BATCH waiting: the rest go in the next batch
            if not jobs:
                continue
            t = time.perf_counter()
            try:
                results = self.detect_batch([frame for _, (frame, _) in jobs])
            except Exception:
                log.exception("people detection failed")
                continue
            ms_per_frame = (time.perf_counter() - t) * 1000 / len(jobs)
            for (_, (_, callback)), boxes in zip(jobs, results):
                callback(boxes, ms_per_frame)
            self.batches += 1
            self.frames += len(jobs)
            if self.batches % 500 == 0:
                log.info("YOLO: %d batches, %.1f frames per batch on average",
                         self.batches, self.frames / self.batches)
