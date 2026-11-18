"""Motion detection with background subtraction.

Idea: OpenCV slowly learns what the "empty" scene looks like (the background).
Pixels in a new frame that differ from that background are foreground = motion.

It runs on a smaller copy of the frame (work_width = 320 px): MOG2 models every pixel, so
half the width = 4x fewer pixels. On the 2-vCPU Azure VM this cut the CPU use of the whole
app by ~30%. Moving people are still big enough at that size. Boxes are scaled back to the
full frame; the motion ratio is a fraction of the image, so it means the same at any size.
"""
import cv2

WARMUP_FRAMES = 50


class MotionDetector:
    def __init__(self, min_area=400, work_width=320):
        # MOG2 = Mixture of Gaussians: models each pixel's normal color range.
        # detectShadows=True marks shadows as gray (127) so we can ignore them.
        self.bg = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=25, detectShadows=True)
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))  # (5, 5) at 640 px
        self.min_area = min_area  # ignore tiny blobs (noise, compression artifacts), full-size pixels
        self.work_width = work_width
        self.frames_seen = 0

    def update(self, frame):
        """Returns (motion_ratio 0..1, list of boxes (x, y, w, h) around moving areas)."""
        h, w = frame.shape[:2]
        scale = min(1.0, self.work_width / float(w))
        if scale < 1.0:
            frame = cv2.resize(frame, (self.work_width, int(h * scale)), interpolation=cv2.INTER_AREA)
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
        min_area = self.min_area * scale * scale  # same blob size limit, in small-image pixels
        boxes = [cv2.boundingRect(c) for c in contours if cv2.contourArea(c) >= min_area]
        boxes = [tuple(int(v / scale) for v in b) for b in boxes]  # back to full-size pixels
        return motion_ratio, boxes
