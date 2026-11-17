"""Person detection with YOLOv8n (ultralytics).

YOLO is a neural network pretrained on the COCO dataset (80 object types).
We only keep class 0 = "person".

One model is shared by all camera threads. A lock makes sure only one thread
uses it at a time: this saves memory, and on a CPU-only VM running several
detections at once would not be faster anyway.
"""
import threading

from ultralytics import YOLO

PERSON_CLASS = 0


class PeopleDetector:
    def __init__(self, model_path="yolov8n.pt", confidence=0.4):
        self.model = YOLO(model_path)  # downloads the 6 MB weights file the first time
        self.confidence = confidence
        self.lock = threading.Lock()

    def detect(self, frame):
        """Returns a list of boxes (x1, y1, x2, y2, confidence) for people in the frame."""
        with self.lock:
            results = self.model(frame, classes=[PERSON_CLASS], conf=self.confidence, verbose=False)
        boxes = []
        for box in results[0].boxes:
            x1, y1, x2, y2 = (int(v) for v in box.xyxy[0].tolist())
            boxes.append((x1, y1, x2, y2, float(box.conf[0])))
        return boxes
