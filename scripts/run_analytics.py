"""Step 2 test: run all cameras with full analytics and check every alert type.

Usage:
    python -m scripts.run_analytics [seconds]

Extra test cameras:
  - "tamper": a generated video that is normal, then dark (covered lens), then blurred
  - "broken": a missing file (offline alert)
Outputs: logs/step2_analytics.log, results/step2_*.jpg, results/step2_events.csv,
         results/step2_benchmark.md
"""
import logging
import os
import platform
import sys
import time

import cv2

from app.analytics.people import PeopleDetector
from app.config import load_config, setup_logging
from app.events import EventStore
from app.cameras.worker import CameraWorker

TAMPER_VIDEO = "data/tamper_test.mp4"


def make_tamper_video(src="videos/people-detection.mp4", seconds_each=6):
    """Normal -> dark -> blurry, `seconds_each` seconds per part."""
    cap = cv2.VideoCapture(src)
    fps = cap.get(cv2.CAP_PROP_FPS) or 12
    out = None
    for part in ("normal", "dark", "blurry"):
        for _ in range(int(fps * seconds_each)):
            ok, f = cap.read()
            if not ok:
                break
            f = cv2.resize(f, (640, 360))
            if part == "dark":
                f = (f * 0.05).astype("uint8")
            elif part == "blurry":
                f = cv2.GaussianBlur(f, (25, 25), 0)
            if out is None:
                out = cv2.VideoWriter(TAMPER_VIDEO, cv2.VideoWriter_fourcc(*"mp4v"), fps, (640, 360))
            out.write(f)
    out.release()


def main():
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    setup_logging("step2_analytics.log")
    log = logging.getLogger("main")
    os.makedirs("data", exist_ok=True)
    os.makedirs("results", exist_ok=True)

    config = load_config()
    a = config["analytics"]
    make_tamper_video()

    db_path = "data/step2_test_events.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    events = EventStore(db_path, cooldown_s=a["alert_cooldown_s"])
    detector = PeopleDetector(a["model"], a["person_confidence"])

    cameras = config["cameras"] + [
        {"id": "tamper", "name": "Tamper test", "source": TAMPER_VIDEO, "crowd_limit": 10},
        {"id": "broken", "name": "Missing file", "source": "videos/does-not-exist.mp4"},
    ]
    workers = [CameraWorker(cam, a, detector, events) for cam in cameras]
    cpu_start, wall_start = time.process_time(), time.monotonic()
    for w in workers:
        w.start()
    log.info("started %d workers with analytics, running for %ds", len(workers), duration)

    while time.monotonic() - wall_start < duration:
        time.sleep(10)
        for w in workers:
            s = w.get_stats()
            log.info("%-6s %-7s fps=%-3d people=%-2d motion=%5.1f%% bright=%-3d sharp=%-5d alerts=%s",
                     s["id"], s["status"], s["fps"], s["people"], s["motion_pct"],
                     s["brightness"], s["sharpness"], ",".join(s["alerts"]) or "-")

    cpu_cores_used = (time.process_time() - cpu_start) / (time.monotonic() - wall_start)
    stats = [w.get_stats() for w in workers]
    for w in workers:
        frame = w.get_frame()
        if frame is not None:
            cv2.imwrite("results/step2_%s.jpg" % w.cam_id, frame)
        w.stop()
    for w in workers:
        w.join(timeout=3)

    n = events.export_csv("results/step2_events.csv")
    log.info("exported %d events to results/step2_events.csv", n)
    counts = {}
    for e in events.recent(1000):
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    log.info("events by type: %s", counts)

    with open("results/step2_benchmark.md", "w") as f:
        f.write("# Step 2 benchmark\n\n")
        f.write("Machine: %s %s, Python %s. Run length: %d s, %d cameras (incl. 2 test cameras).\n\n"
                % (platform.system(), platform.machine(), platform.python_version(), duration, len(workers)))
        f.write("**Average CPU use of the whole program: %.2f cores**\n\n" % cpu_cores_used)
        f.write("| Camera | FPS | Motion ms | Quality ms | People (YOLO) ms |\n|---|---|---|---|---|\n")
        for s in stats:
            t = s["timing_ms"]
            f.write("| %s | %d | %.1f | %.1f | %.1f |\n" % (s["id"], s["fps"], t["motion"], t["quality"], t["people"]))
        f.write("\nEvents by type: %s\n" % counts)
    log.info("CPU use: %.2f cores; benchmark saved to results/step2_benchmark.md", cpu_cores_used)


if __name__ == "__main__":
    main()
