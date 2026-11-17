"""Video sources: where frames come from.

Every source has the same small interface:
    open()       -> True if the source could be opened
    read()       -> next frame (NumPy image) or None if nothing is available
    close()

Workers only use this interface, so a different source type
(e.g. a phone pushing frames) can be added later without changing the workers.
"""
import time

import cv2


class FileSource:
    """Plays a video file like a live camera: at real speed, looping forever."""

    def __init__(self, path):
        self.path = path
        self.cap = None
        self.fps = 25.0
        self.next_frame_time = 0.0

    def open(self):
        self.cap = cv2.VideoCapture(self.path)
        if not self.cap.isOpened():
            return False
        # Use the file's own frame rate so playback looks like real time.
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 25.0
        self.next_frame_time = time.monotonic()
        return True

    def read(self):
        ok, frame = self.cap.read()
        if not ok:
            # End of the file: jump back to the first frame -> endless "live" video.
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
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


def make_source(spec):
    """Create a source from the 'source' value in config.yaml."""
    return FileSource(spec)
