"""Server-lifecycle polling of durable schedules; OS leases prevent duplicate work."""

from __future__ import annotations

import fcntl
import os
import sqlite3
import threading
from contextlib import contextmanager

from openjarvis.connectors.source_store import SourceConflict


class SourceJobRunner:
    def __init__(self, manager):
        self.manager = manager
        self.stop_event = threading.Event()
        self.thread = None

    @contextmanager
    def _leader(self):
        fd = os.open(
            self.manager.store.path.with_suffix(".scheduler.lock"),
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def tick(self, now=None):
        try:
            with self._leader():
                for job in self.manager.jobs.active(state="running"):
                    if self.stop_event.is_set():
                        return
                    try:
                        with (
                            self.manager._job_locked(job["id"]),
                            self.manager._locked(job["source_id"]),
                        ):
                            if self.manager.jobs.get(job["id"])["state"] == "running":
                                self.manager.jobs.finish(
                                    job["id"],
                                    "interrupted",
                                    "Sync interrupted; retry uses the "
                                    "previous successful token",
                                )
                    except (SourceConflict, KeyError, ValueError):
                        pass
                for job in self.manager.jobs.active(state="queued"):
                    if self.stop_event.is_set():
                        return
                    try:
                        self.manager.start_sync(job["source_id"], job_id=job["id"])
                    except (SourceConflict, KeyError, ValueError):
                        pass
                for schedule in self.manager.jobs.due(now):
                    if self.stop_event.is_set():
                        return
                    try:
                        self.manager.start_sync(
                            schedule["source_id"], trigger="scheduled", now=now
                        )
                    except (SourceConflict, KeyError, ValueError):
                        pass  # Capacity/busy schedules remain due for the next tick.
        except BlockingIOError:
            pass  # Another server process owns this tick's scheduler lease.

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()

        def run():
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except (OSError, RuntimeError, sqlite3.Error):
                    # A temporary database/storage problem is retried next tick;
                    # never log remote content or decrypted credential material.
                    pass
                self.stop_event.wait(1)

        self.thread = threading.Thread(target=run, name="source-scheduler", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
