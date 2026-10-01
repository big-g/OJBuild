"""Ephemeral cooperative controls; no credentials or remote data in progress."""

from __future__ import annotations

import time


class SyncCancelled(RuntimeError):
    def __init__(self):
        super().__init__("Sync cancelled")


class JobControl:
    def __init__(self, jobs, identity):
        self.jobs = jobs
        self.identity = identity
        self.values = {}
        self._last_report = 0.0

    def is_set(self):
        return self.jobs.get(self.identity)["cancel_requested"]

    def check(self):
        if self.is_set():
            raise SyncCancelled()

    def report(self, *, force=False, **values):
        self.values.update(values)
        now = time.monotonic()
        if force or now - self._last_report >= 0.25:
            self.jobs.progress(self.identity, **self.values)
            self._last_report = now
