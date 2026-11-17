"""Video sources: where frames come from.

Every source has the same small interface:
    open()       -> True if the source could be opened
    read()       -> next frame (NumPy image) or None if nothing is available
    close()

Workers only use this interface, so a different source type can be added
without changing the workers. Two types exist:
    FileSource  - a video file played like a live camera ("source: videos/x.mp4")
    PushSource  - a phone/browser pushing JPEG frames to the server ("source: push")
"""
import threading
import time

import cv2
import numpy as np


class FileSource:
    """Plays a video file like a live camera: at real speed, looping forever."""

    def __init__(self, path, max_fps=None):
        self.path = path
        self.max_fps = max_fps
        self.cap = None
        self.fps = 25.0
        self.skip = 1
        self.next_frame_time = 0.0

    def open(self):
        self.cap = cv2.VideoCapture(self.path)
        if not self.cap.isOpened():
            return False
        # Use the file's own frame rate so playback looks like real time.
        file_fps = self.cap.get(cv2.CAP_PROP_FPS) or 25.0
        # A 60 fps video with max_fps=15 -> use every 4th frame. Monitoring doesn't need
        # 60 fps, and every frame we skip saves analytics CPU time.
        if self.max_fps and file_fps > self.max_fps:
            self.skip = int(round(file_fps / self.max_fps))
        self.fps = file_fps / self.skip
        self.next_frame_time = time.monotonic()
        return True

    def _rewind_if_ended(self, ok):
        # End of the file: jump back to the first frame -> endless "live" video.
        if not ok:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        return ok

    def read(self):
        for _ in range(self.skip - 1):
            self._rewind_if_ended(self.cap.grab())  # grab() skips a frame cheaply
        ok, frame = self.cap.read()
        if not self._rewind_if_ended(ok):
            ok, frame = self.cap.read()
            if not ok:
                return None  # file is broken or was removed

        # Wait until it is time to show this frame. Without this, OpenCV would read
        # the whole file in a few seconds instead of playing it at normal speed.
        self.next_frame_time += 1.0 / self.fps
        delay = self.next_frame_time - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            # We are behind (e.g. slow analytics); don't try to catch up, just continue.
            self.next_frame_time = time.monotonic()
        return frame

    def close(self):
        if self.cap is not None:
            self.cap.release()


class PushHub:
    """Mailbox between the web server and the camera workers for pushed cameras.

    The Flask route that receives a frame from the phone calls put(); the camera
    worker waits in get(). Only the NEWEST frame is kept: if analytics are slower
    than the phone, old frames are dropped instead of piling up (low latency).
    Frames are stored as JPEG bytes and decoded in the worker thread, so the HTTP
    request returns quickly.
    """

    def __init__(self):
        self.cond = threading.Condition()
        self.frames = {}  # cam_id -> (seq, received_at, jpeg_bytes)

    def put(self, cam_id, jpeg):
        with self.cond:
            seq = self.frames.get(cam_id, (0, 0, None))[0] + 1
            self.frames[cam_id] = (seq, time.monotonic(), jpeg)
            self.cond.notify_all()
        return seq

    def last_time(self, cam_id):
        with self.cond:
            return self.frames.get(cam_id, (0, 0.0, None))[1]

    def get(self, cam_id, after_seq, timeout):
        """Wait for a frame newer than `after_seq`. Returns (seq, jpeg) or (after_seq, None)."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                seq, _, jpeg = self.frames.get(cam_id, (0, 0, None))
                if seq > after_seq:
                    return seq, jpeg
                left = deadline - time.monotonic()
                if left <= 0:
                    return after_seq, None
                self.cond.wait(left)


push_hub = PushHub()  # one shared hub for the whole process


class PushSource:
    """A camera whose frames are pushed to the server over HTTP (e.g. an iPhone
    streaming from the /push page). "Opened" = a frame arrived recently."""

    def __init__(self, cam_id, timeout_s=5, hub=None):
        self.cam_id = cam_id
        self.timeout_s = timeout_s
        self.hub = hub or push_hub
        self.seq = 0
        self.fps = 0.0  # unknown: depends on the phone and the network

    def open(self):
        # Not streaming yet -> report "cannot open", the worker retries every few seconds.
        last = self.hub.last_time(self.cam_id)
        return last > 0 and time.monotonic() - last < self.timeout_s

    def read(self):
        while True:
            self.seq, jpeg = self.hub.get(self.cam_id, self.seq, self.timeout_s)
            if jpeg is None:
                return None  # phone stopped sending -> worker marks the camera offline
            frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if frame is not None:
                return frame
            # broken JPEG: skip it and wait for the next one

    def close(self):
        pass


def is_push(spec):
    return str(spec).strip().lower() == "push"


def make_source(spec, max_fps=None, cam_id=None, timeout_s=5):
    """Create a source from the 'source' value in config.yaml."""
    if is_push(spec):
        return PushSource(cam_id, timeout_s)
    return FileSource(spec, max_fps)
