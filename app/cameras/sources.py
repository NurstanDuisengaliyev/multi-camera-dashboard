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
import threading
import time

import av
import cv2
import numpy as np


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
    MediaMTX offers every stream as RTSP too, and we decode it with PyAV (Python + ffmpeg).

    Why not cv2.VideoCapture like the video files? WebRTC changes the resolution while
    streaming (the phone sends a smaller picture when the connection gets worse, or it was
    rotated). OpenCV can't handle that: it kept returning the last good picture forever
    (a frozen image at 16 fps). PyAV decodes every frame at its own size.

    A background thread decodes everything as it arrives and keeps only the NEWEST frame
    (like the FrameInbox below for JPEG uploads). If the analytics are slower than the phone
    (busy server, several phones), older frames are skipped instead of piling up in the
    network connection, where they made the video fall further and further behind.
    """

    retry_delay_s = 1  # the phone may not be connected yet: try again soon
    WIDTH = 640        # every frame is delivered this wide (see _steady)
    TIMEOUT_S = 3      # no frame for this long: the phone stopped

    def __init__(self, url):
        self.url = url
        self.container = None
        self.size = None   # (width, height) we deliver; stays the same while streaming
        self.fps = None    # decided by the phone
        self.cond = threading.Condition()
        self.newest = None  # newest decoded frame (PyAV VideoFrame), set by the thread
        self.number = 0     # frames decoded so far
        self.taken = 0      # number of the frame read() gave out last
        self.alive = False
        self.closing = False

    def open(self):
        try:
            self.container = av.open(self.url, timeout=(2.0, self.TIMEOUT_S), options={
                "rtsp_transport": "tcp",
                "fflags": "nobuffer", "flags": "low_delay",          # no buffering
                "analyzeduration": "500000", "probesize": "100000"})  # start within ~0.5 s
        except (av.error.FFmpegError, OSError):
            return False  # no stream yet (phone not connected): the worker says "waiting"
        # Decode with several cores by splitting each frame ("SLICE"), not by working on several
        # frames at once ("FRAME"/"AUTO"): that holds ~2 frames back (+130 ms delay, measured).
        self.container.streams.video[0].thread_type = "SLICE"
        self.alive = True
        threading.Thread(target=self._receive, name="rtsp-" + self.url.rsplit("/", 1)[-1], daemon=True).start()
        return True

    def _receive(self):
        """Background thread: decode every packet (H.264 needs all of them), keep the newest frame."""
        try:
            for packet in self.container.demux(video=0):
                if self.closing:
                    break
                try:
                    frames = packet.decode()
                except av.error.FFmpegError:
                    continue  # damaged by lost network packets: skip, the next one is fine
                if frames:
                    with self.cond:
                        self.newest = frames[-1]
                        self.number += 1
                        self.cond.notify_all()
        except (av.error.FFmpegError, OSError):
            pass  # nothing for TIMEOUT_S seconds, or the stream ended
        finally:
            self.container.close()  # closed by this thread: it is the only one using it
            with self.cond:
                self.alive = False
                self.cond.notify_all()

    def read(self):
        with self.cond:
            self.cond.wait_for(lambda: self.number > self.taken or not self.alive, self.TIMEOUT_S)
            if self.number <= self.taken:
                return None  # phone stopped
            frame, self.taken = self.newest, self.number  # frames in between are skipped
        return self._steady(frame.to_ndarray(format="bgr24"))

    def _steady(self, img):
        """Always deliver the SAME picture size, whatever WebRTC does: it starts with a small
        picture and raises the resolution once it has measured the connection, and lowers it
        when the connection gets worse. So every frame is scaled to WIDTH px wide (what the
        analytics use anyway, config.yaml resize_width); the height comes from the picture's
        shape and only changes when the phone is rotated (portrait <-> landscape)."""
        h, w = img.shape[:2]
        if self.size is None or (w > h) != (self.size[0] > self.size[1]):
            height = int(round(self.WIDTH * h / w / 2)) * 2  # even: needed by the H.264 encoder
            self.size = (self.WIDTH, height)
        if (w, h) != self.size:
            img = cv2.resize(img, self.size, interpolation=cv2.INTER_AREA if w > self.WIDTH else cv2.INTER_LINEAR)
        return img

    def close(self):
        self.closing = True  # the thread stops after the next packet (or its timeout)


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
