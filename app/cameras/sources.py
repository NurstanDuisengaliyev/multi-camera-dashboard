"""Video sources: where a camera's frames come from.

Every source has the same small interface, so the CameraWorker doesn't care which one it has:
    open()   -> True when frames are available
    read()   -> next frame (NumPy image), or None when the source stopped
    close()
    retry_delay_s   how long the worker waits before calling open() again after a failure

Three kinds of source:
    FileSource   a video file played like a live camera (cameras listed in config.yaml)
    RtspSource   a phone's WebRTC video, read from MediaMTX (which offers it as RTSP)
    PhoneSource  JPEG frames uploaded by a phone over HTTPS (fallback when WebRTC is not possible)
"""
import os
import threading
import time

# Read live RTSP streams with as little buffering as possible: always the newest frame.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay")

import cv2  # noqa: E402 (must come after the environment variable above)
import numpy as np  # noqa: E402


class FileSource:
    """Plays a video file like a live camera: at real speed, looping forever."""

    retry_delay_s = 2

    def __init__(self, path, max_fps=None, speed=1.0):
        self.path = path
        self.max_fps = max_fps
        self.speed = speed  # 1.0 = the file's own frame rate; 0.7 = 30% slower (see config.yaml)
        self.cap = None
        self.fps = 25.0
        self.skip = 1
        self.next_frame_time = 0.0

    def open(self):
        self.cap = cv2.VideoCapture(self.path)
        if not self.cap.isOpened():
            return False
        # Use the file's own frame rate so playback looks like real time. Some clips were
        # recorded with frames left out and look too fast at that rate: `speed` corrects it.
        file_fps = (self.cap.get(cv2.CAP_PROP_FPS) or 25.0) * self.speed
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


class RtspSource:
    """A phone's WebRTC video. The phone sends it to MediaMTX (WHIP, see static/phone.js);
    MediaMTX offers every stream as RTSP too, and OpenCV reads RTSP like a video file."""

    retry_delay_s = 1  # the phone may not be connected yet: try again soon

    def __init__(self, url):
        self.url = url
        self.cap = None
        self.fps = None  # decided by the phone

    def open(self):
        self.cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG, [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 2000,   # no stream yet -> fail fast, worker says "waiting"
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, 3000])  # phone stopped -> read() returns None
        return self.cap.isOpened()

    def read(self):
        ok, frame = self.cap.read()
        return frame if ok else None

    def close(self):
        if self.cap is not None:
            self.cap.release()


class FrameInbox:
    """Mailbox between the web server (receives frames from phones) and the camera
    workers (analyze them).

    Only the NEWEST frame per camera is kept: if analytics are slower than the phone,
    old frames are dropped instead of piling up, so the dashboard always shows "now".
    Frames are stored as JPEG bytes and decoded later in the worker thread, so the
    upload request from the phone returns quickly.
    """

    def __init__(self):
        self.cond = threading.Condition()
        self.frames = {}  # cam_id -> (frame number, time received, jpeg bytes)

    def put(self, cam_id, jpeg):
        with self.cond:
            number = self.frames.get(cam_id, (0, 0, None))[0] + 1
            self.frames[cam_id] = (number, time.monotonic(), jpeg)
            self.cond.notify_all()  # wake up the worker waiting in wait_for_frame()

    def wait_for_frame(self, cam_id, newer_than, timeout):
        """Wait up to `timeout` s for a frame numbered above `newer_than`.
        Returns (number, jpeg), or (newer_than, None) on timeout."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                number, _, jpeg = self.frames.get(cam_id, (0, 0, None))
                if number > newer_than:
                    return number, jpeg
                left = deadline - time.monotonic()
                if left <= 0:
                    return newer_than, None
                self.cond.wait(left)

    def forget(self, cam_id):
        with self.cond:
            self.frames.pop(cam_id, None)


class PhoneSource:
    """Frames uploaded by a phone. "Open" means: the phone is sending right now."""

    retry_delay_s = 0  # open() already waits for the phone, no extra pause needed

    def __init__(self, cam_id, inbox, timeout_s=5):
        self.cam_id = cam_id
        self.inbox = inbox
        self.timeout_s = timeout_s
        self.last_number = 0
        self.fps = None  # decided by the phone, not by us

    def open(self):
        # Wait a moment for the phone. If nothing comes, the worker reports "waiting".
        number, _ = self.inbox.wait_for_frame(self.cam_id, self.last_number, timeout=1.0)
        return number > self.last_number

    def read(self):
        while True:
            self.last_number, jpeg = self.inbox.wait_for_frame(self.cam_id, self.last_number,
                                                               self.timeout_s)
            if jpeg is None:
                return None  # phone stopped sending
            frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if frame is not None:
                return frame
            # broken JPEG: skip it and wait for the next one

    def close(self):
        pass
