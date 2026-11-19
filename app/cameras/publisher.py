"""WebRtcPublisher: sends one camera's analyzed video (boxes drawn) to MediaMTX as H.264.

    worker.latest_frame --raw pixels--> ffmpeg (H.264 encoder) --RTSP--> MediaMTX --WebRTC--> browsers

Why H.264 instead of a JPEG per frame (the /frame fallback in app/web/dashboard.py)?
A JPEG is a complete picture every time (15-50 KB). H.264 sends a complete picture once per
second and in between only what changed, which is usually much less: ~10x less data
for the same video. That's what makes video calls smooth on slow connections.

The video is compressed ONCE per camera, however many browsers watch: MediaMTX only
forwards the compressed packets. One thread + one ffmpeg process per camera.
"""
import logging
import subprocess
import threading
import time

STOP_AFTER_S = 3  # no new frame for this long (camera offline, phone stopped): end the stream


def ffmpeg_command(width, height, url, kbps):
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        # Input: raw BGR pixels (OpenCV's format) on stdin. Each frame gets the time it
        # arrived as its timestamp, so the video plays at the camera's real speed.
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", "%dx%d" % (width, height),
        "-use_wallclock_as_timestamps", "1", "-i", "pipe:0",
        # Output: H.264 the way browsers play it in WebRTC (baseline profile, yuv420p),
        # tuned for live video: fastest preset, no frames held back (zerolatency).
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-profile:v", "baseline", "-pix_fmt", "yuv420p",
        "-b:v", "%dk" % kbps, "-maxrate", "%dk" % kbps, "-bufsize", "%dk" % (kbps // 2),
        # A complete picture every second: a browser that just connected waits at most 1 s.
        "-force_key_frames", "expr:gte(t,n_forced*1)",
        "-fps_mode", "passthrough",
        "-f", "rtsp", "-rtsp_transport", "tcp", url,
    ]


class WebRtcPublisher(threading.Thread):
    def __init__(self, worker, url, kbps=800):
        super().__init__(name=worker.cam_id + "-webrtc", daemon=True)
        self.worker = worker
        self.url = url
        self.kbps = kbps
        self.log = logging.getLogger(worker.cam_id)
        self.proc = None
        self.size = None
        self.retry_at = 0.0

    def run(self):
        number = 0
        while not self.worker.stopped:
            frame, new_number = self.worker.wait_frame(number, timeout=STOP_AFTER_S)
            if frame is None:
                self._stop_ffmpeg()  # viewers see the stream end and switch to the JPEG fallback
                continue
            number = new_number
            h, w = frame.shape[:2]
            h, w = h - h % 2, w - w % 2  # yuv420p needs an even width and height
            if self.proc is None or self.size != (w, h):  # first frame, or the size changed
                if time.monotonic() < self.retry_at:
                    continue
                self._stop_ffmpeg()
                self._start_ffmpeg(w, h)
                if self.proc is None:
                    continue
            try:
                self.proc.stdin.write(frame[:h, :w].tobytes())
            except (BrokenPipeError, OSError):
                self.log.warning("WebRTC stream: ffmpeg stopped (MediaMTX not reachable?), retry in 5 s")
                self._stop_ffmpeg()
                self.retry_at = time.monotonic() + 5
        self._stop_ffmpeg()

    def _start_ffmpeg(self, w, h):
        try:
            self.proc = subprocess.Popen(ffmpeg_command(w, h, self.url, self.kbps), stdin=subprocess.PIPE)
        except FileNotFoundError:
            self.log.error("WebRTC stream: ffmpeg is not installed (it is in the Docker image)")
            self.retry_at = time.monotonic() + 60
            return
        self.size = (w, h)
        self.log.info("WebRTC stream started: %dx%d H.264 %d kbit/s -> %s", w, h, self.kbps, self.url)

    def _stop_ffmpeg(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
        self.proc = None
        self.log.info("WebRTC stream stopped")
