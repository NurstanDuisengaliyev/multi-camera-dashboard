"""Image quality checks used to detect camera tampering.

- brightness: average gray value (0 = black, 255 = white).
  Very low -> lens covered, lights off, or camera pointed at a wall.
- sharpness: variance of the Laplacian (an edge detector).
  Sharp images have many strong edges -> high variance.
  Very low -> blurry image (dirty/smeared lens, out of focus).
"""
import cv2


def measure(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    brightness = float(gray.mean())
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    return brightness, sharpness
