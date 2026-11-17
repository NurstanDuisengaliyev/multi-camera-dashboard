"""Motion detection with background subtraction.

Idea: OpenCV slowly learns what the "empty" scene looks like (the background).
Pixels in a new frame that differ from that background are foreground = motion.
"""
import cv2

WARMUP_FRAMES = 50


class MotionDetector:
    def __init__(self, min_area=400):
        # MOG2 = Mixture of Gaussians: models each pixel's normal color range.
        # detectShadows=True marks shadows as gray (127) so we can ignore them.
        self.bg = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=25, detectShadows=True)
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        self.min_area = min_area  # ignore tiny blobs (noise, compression artifacts)
        self.frames_seen = 0

    def update(self, frame):
        """Returns (motion_ratio 0..1, list of boxes (x, y, w, h) around moving areas)."""
        mask = self.bg.apply(frame)
        self.frames_seen += 1
        if self.frames_seen < WARMUP_FRAMES:
            # The background model is still learning: at first EVERYTHING looks like
            # motion, so we report nothing until it has seen enough frames.
            return 0.0, []
        # Keep only real foreground (255), drop shadows (127).
        _, mask = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)
        # Remove salt noise, then grow blobs a bit so one person = one blob.
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.kernel)
        mask = cv2.dilate(mask, self.kernel, iterations=2)

        motion_ratio = cv2.countNonZero(mask) / float(mask.size)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes = [cv2.boundingRect(c) for c in contours if cv2.contourArea(c) >= self.min_area]
        return motion_ratio, boxes
