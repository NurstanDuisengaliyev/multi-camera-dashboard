"""Step 1 test: start one worker per camera and print FPS/status.

Usage:
    python -m scripts.run_workers [seconds]

Also adds a fake camera with a missing file to prove offline detection works,
and saves one snapshot per camera to results/.
"""
import logging
import os
import sys
import time

import cv2

from app.config import load_config, setup_logging
from app.worker import CameraWorker


def main():
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    setup_logging("step1_workers.log")
    log = logging.getLogger("main")

    config = load_config()
    cameras = config["cameras"] + [
        {"id": "broken", "name": "Missing file (offline test)", "source": "videos/does-not-exist.mp4"}
    ]
    workers = [CameraWorker(cam, config["analytics"]) for cam in cameras]
    for w in workers:
        w.start()
    log.info("started %d workers, running for %ds", len(workers), duration)

    start = time.monotonic()
    while time.monotonic() - start < duration:
        time.sleep(5)
        for w in workers:
            s = w.get_stats()
            log.info("%-7s %-30s status=%-7s fps=%-3d frames=%d",
                     s["id"], s["name"], s["status"], s["fps"], s["frames_total"])

    os.makedirs("results", exist_ok=True)
    for w in workers:
        frame = w.get_frame()
        if frame is not None:
            path = "results/step1_%s.jpg" % w.cam_id
            cv2.imwrite(path, frame)
            log.info("saved snapshot %s (%dx%d)", path, frame.shape[1], frame.shape[0])
        w.stop()
    for w in workers:
        w.join(timeout=3)
    log.info("all workers stopped")


if __name__ == "__main__":
    main()
