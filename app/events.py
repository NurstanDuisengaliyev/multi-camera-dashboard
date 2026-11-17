"""Event log: alerts are saved to SQLite and written to the log.

A cooldown per (camera, event type) prevents the same alert from flooding the
log, e.g. "motion detected" at most once every 30 seconds per camera.
"""
import csv
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime

log = logging.getLogger("events")


class EventStore:
    def __init__(self, db_path="data/events.db", cooldown_s=30):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        # check_same_thread=False: all camera threads share this connection; the lock keeps it safe.
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS events (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts REAL, time TEXT, camera_id TEXT, camera_name TEXT,
                   type TEXT, message TEXT, value REAL)"""
        )
        self.db.commit()
        self.lock = threading.Lock()
        self.cooldown_s = cooldown_s
        self.last_sent = {}  # (camera_id, type) -> time of the last saved event

    def add(self, camera_id, camera_name, event_type, message, value=None, cooldown=True):
        """Save an event. Returns False if it was skipped because of the cooldown."""
        now = time.time()
        key = (camera_id, event_type)
        with self.lock:
            if cooldown and now - self.last_sent.get(key, 0) < self.cooldown_s:
                return False
            self.last_sent[key] = now
            self.db.execute(
                "INSERT INTO events (ts, time, camera_id, camera_name, type, message, value) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now, datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
                 camera_id, camera_name, event_type, message, value),
            )
            self.db.commit()
        log.warning("[%s] %s: %s", camera_id, event_type, message)
        return True

    def recent(self, limit=50):
        with self.lock:
            rows = self.db.execute(
                "SELECT time, camera_id, camera_name, type, message, value "
                "FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        keys = ("time", "camera_id", "camera_name", "type", "message", "value")
        return [dict(zip(keys, r)) for r in rows]

    def export_csv(self, path):
        with self.lock:
            rows = self.db.execute(
                "SELECT time, camera_id, camera_name, type, message, value FROM events ORDER BY id"
            ).fetchall()
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["time", "camera_id", "camera_name", "type", "message", "value"])
            writer.writerows(rows)
        return len(rows)
