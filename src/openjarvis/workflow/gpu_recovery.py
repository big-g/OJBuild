"""Bounded maintenance agent for interrupted local image GPU leases.

No model decides which processes to stop. Only the fixed, dedicated ComfyUI
user service can be restarted, under the same exclusive lock as generation.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from openjarvis.workflow import providers

logger = logging.getLogger(__name__)
LOCAL_WORKER = "http://127.0.0.1:8188"
OLLAMA = "http://127.0.0.1:11434"


class GPURecovery:
    def __init__(self):
        self.stop = threading.Event()
        self.thread = None
        self.failures = 0
        self.identity = None

    def start(self):
        if os.environ.get("OPENJARVIS_GPU_LOCK_PATH") and self.thread is None:
            self.thread = threading.Thread(
                target=self._run, name="gpu-recovery", daemon=True
            )
            self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _run(self):
        while not self.stop.wait(15):
            try:
                self.tick()
            except Exception:
                logger.exception("GPU recovery diagnostic failed; reservation retained")

    def tick(self):
        path = os.environ.get("OPENJARVIS_GPU_LOCK_PATH")
        if not path or self.stop.is_set():
            return
        marker = Path(path + ".blocked")
        if not marker.exists():
            self.failures = 0
            return
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return  # A chat, image, or 3D operation still owns this GPU.
            if not marker.exists():
                return
            handle = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(handle) as source:
                try:
                    state = json.loads(source.read(16384))
                except ValueError:
                    return  # Legacy/foreign markers require explicit recovery.
            if (
                not isinstance(state, dict)
                or state.get("owner") != "openjarvis-image-v1"
            ):
                return
            # Never use a marker as an arbitrary URL or command source.
            if state.get("url") != LOCAL_WORKER:
                return
            baseline = {int(k): tuple(v) for k, v in state["baseline"].items()}
            if not baseline or any(
                k < 0 or len(v) != 2 or any(type(n) is not int or n < 0 for n in v)
                for k, v in baseline.items()
            ):
                raise ValueError("Invalid GPU recovery baseline")
            identity = state.get("created")
            if identity != self.identity:
                self.identity, self.failures = identity, 0
            if time.time() - state.get("last_restart", 0) < 120:
                return
            try:
                models = providers.request(OLLAMA, "/api/ps").get("models")
                if not isinstance(models, list):
                    raise ValueError("Invalid Ollama model statistics")
                # Under the exclusive lease there can be no coordinated chat.
                for model in models:
                    providers.request(
                        OLLAMA,
                        "/api/generate",
                        payload={
                            "model": model["name"],
                            "keep_alive": 0,
                            "stream": False,
                        },
                    )
                if providers.request(OLLAMA, "/api/ps").get("models") != []:
                    logger.warning("GPU recovery waiting for Ollama unload")
                    return
            except Exception:
                logger.exception("Ollama handoff unavailable; GPU reservation retained")
                return
            try:
                queue = providers.request(LOCAL_WORKER, "/queue")
                if any(
                    not isinstance(queue.get(key), list)
                    for key in ("queue_running", "queue_pending")
                ):
                    raise ValueError("Invalid image queue statistics")
                if queue["queue_running"] or queue["queue_pending"]:
                    self.failures = 0
                    return
                ready, snapshot = providers.cleanup_snapshot(LOCAL_WORKER, baseline)
                logger.info("GPU recovery diagnostics: %s", snapshot)
                if snapshot["running"] or snapshot["pending"]:
                    self.failures = 0
                    return  # Do not interrupt queued or externally submitted work.
                providers.request(
                    LOCAL_WORKER,
                    "/free",
                    payload={"unload_models": True, "free_memory": True},
                    acknowledgement=True,
                )
                if self.stop.wait(2):
                    return
                verified, snapshot = providers.cleanup_snapshot(LOCAL_WORKER, baseline)
                if ready and verified and not self.stop.is_set():
                    marker.unlink()
                    self.failures = 0
                    logger.info(
                        "GPU recovery verified; GPU available again: %s", snapshot
                    )
                    return
            except Exception:
                logger.exception("GPU recovery health check failed")
            self.failures += 1
            if self.failures < 3 or self.stop.is_set():
                return
            # Persist the retry budget before attempting the fixed service action.
            if state.get("restarts", 0) >= 3:
                logger.error(
                    "GPU recovery restart budget exhausted; administrator required"
                )
                return
            if os.environ.get("OPENJARVIS_GPU_AUTO_RESTART", "1") != "1":
                return
            state["restarts"] = state.get("restarts", 0) + 1
            state["last_restart"] = time.time()
            handle = os.open(marker, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
            with os.fdopen(handle, "w") as target:
                json.dump(state, target)
                target.flush()
                os.fsync(target.fileno())
            self.failures = 0
            logger.warning(
                "Restarting dedicated image worker; attempt %s", state["restarts"]
            )
            environment = os.environ.copy()
            environment.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            environment.setdefault(
                "DBUS_SESSION_BUS_ADDRESS",
                "unix:path=" + environment["XDG_RUNTIME_DIR"] + "/bus",
            )
            subprocess.run(
                ["systemctl", "--user", "restart", "openjarvis-image.service"],
                env=environment,
                check=True,
                timeout=45,
                capture_output=True,
            )
            # Restart success is not health proof. Next tick must verify release.
        finally:
            os.close(fd)
