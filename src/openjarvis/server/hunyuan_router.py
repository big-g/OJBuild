"""Owner-scoped image-to-3D jobs backed by the local Hunyuan worker."""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from openjarvis.artifacts.store import _directory
from openjarvis.server.auth import authenticate_request

MAX_IMAGE_BYTES = 20 * 1024**2
MAX_GLB_BYTES = 20 * 1024**2
PUBLIC_FIELDS = (
    "job_id",
    "status",
    "model",
    "seed",
    "vertices",
    "faces",
    "watertight",
    "elapsed_seconds",
    "peak_allocated_vram_gib",
    "background_model",
    "chat_mode",
)


class HunyuanJobs:
    def __init__(self, catalog: Path, inputs: Path, worker_url: str):
        if worker_url.rstrip("/") != "http://127.0.0.1:8090":
            raise ValueError("Hunyuan worker must use http://127.0.0.1:8090")
        self.catalog, self.inputs, self.url = catalog, inputs, worker_url.rstrip("/")
        with _directory(catalog.parent):
            pass
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs "
                "(id TEXT PRIMARY KEY, owner TEXT NOT NULL)"
            )
        os.chmod(catalog, 0o600)

    @contextmanager
    def db(self):
        if self.catalog.is_symlink():
            raise OSError("Unsafe 3D job catalog")
        db = sqlite3.connect(self.catalog, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def own(self, owner, job_id):
        with self.db() as db:
            found = db.execute(
                "SELECT 1 FROM jobs WHERE id=? AND owner=?", (job_id, owner)
            ).fetchone()
        if not found:
            raise HTTPException(404, "3D job not found")

    def call(self, path, **kwargs):
        lock = os.environ.get("OPENJARVIS_GPU_LOCK_PATH")
        if path == "/jobs" and kwargs and lock and Path(lock + ".blocked").exists():
            raise HTTPException(409, "Local GPU needs administrator recovery")
        try:
            with httpx.Client(
                timeout=90, trust_env=False, follow_redirects=False
            ) as client:
                response = client.request(
                    "POST" if kwargs else "GET", self.url + path, **kwargs
                )
            if not response.is_success:
                if response.status_code == 400:
                    detail = response.json().get("detail", "")
                    if isinstance(detail, str) and detail.startswith(
                        "Configured generation chat model"
                    ):
                        raise HTTPException(400, detail)
                if response.status_code == 409:
                    raise HTTPException(
                        409, "GPU busy; finish the active chat or 3D job and retry"
                    )
                raise HTTPException(
                    503, "3D worker unavailable or rejected the request"
                )
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(503, "3D worker unavailable") from exc

    def status(self, owner, job_id):
        self.own(owner, job_id)
        result = self.call("/jobs/" + job_id)
        public = {key: result[key] for key in PUBLIC_FIELDS if key in result}
        if result.get("status") == "failed":
            public["error"] = (
                "Generation failed; administrator can inspect the worker log"
            )
        return public


def create_hunyuan_router(jobs: HunyuanJobs | None, config=None):
    router = APIRouter(prefix="/v1/3d", tags=["3d"])

    def available(owner: str = Depends(authenticate_request)):
        if jobs is None:
            raise HTTPException(503, "Local 3D generation has not been configured")
        return owner

    @router.get("/jobs")
    def list_jobs(owner: str = Depends(available)):
        with jobs.db() as db:
            ids = db.execute(
                "SELECT id FROM jobs WHERE owner=? ORDER BY rowid DESC LIMIT 100",
                (owner,),
            ).fetchall()
        return {
            "jobs": [jobs.status(owner, row[0]) for row in ids],
            "default_model": config.server.hunyuan_model if config else "turbo",
        }

    @router.post("/jobs", status_code=202)
    async def submit(
        request: Request,
        model: Literal["standard", "turbo"] | None = None,
        owner: str = Depends(available),
    ):
        model = model or (config.server.hunyuan_model if config else "turbo")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_IMAGE_BYTES:
                raise HTTPException(413, "Image exceeds 20 MiB")
            body.extend(chunk)
        if not (
            body.startswith(b"\x89PNG\r\n\x1a\n") or body.startswith(b"\xff\xd8\xff")
        ):
            raise HTTPException(400, "Upload a PNG or JPEG image")
        with jobs.db() as db:
            count = db.execute(
                "SELECT count(*) FROM jobs WHERE owner=?", (owner,)
            ).fetchone()[0]
        if count >= 100:
            raise HTTPException(400, "3D job limit reached (100 per account)")
        name = str(uuid.uuid4()) + ".png"
        # Bounded body, generated basename, no symlinks (including ancestors).
        with _directory(jobs.inputs) as fd:
            handle = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=fd,
            )
            with os.fdopen(handle, "wb") as output:
                output.write(body)
        try:
            # Run network I/O off the async event loop.
            from starlette.concurrency import run_in_threadpool

            result = await run_in_threadpool(
                jobs.call,
                "/jobs",
                json={
                    "image_path": str(jobs.inputs / name),
                    "model": model,
                    "seed": 42,
                    "primary_model": (
                        config.server.model or config.intelligence.default_model
                    )
                    if config
                    else "",
                    "background_model": config.server.generation_chat_model
                    if config
                    else "",
                },
            )
            job_id = str(uuid.UUID(result["job_id"]))
            with jobs.db() as db:
                db.execute("INSERT INTO jobs VALUES (?,?)", (job_id, owner))
            return {key: result[key] for key in PUBLIC_FIELDS if key in result}
        finally:
            with _directory(jobs.inputs) as fd:
                os.unlink(name, dir_fd=fd)

    @router.get("/jobs/{job_id}")
    def status(job_id: str, owner: str = Depends(available)):
        return jobs.status(owner, job_id)

    @router.get("/jobs/{job_id}/download")
    def download(job_id: str, owner: str = Depends(available)):
        state = jobs.status(owner, job_id)
        if state.get("status") != "completed":
            raise HTTPException(409, "3D job has not completed")
        data = bytearray()
        try:
            with httpx.Client(
                timeout=90, trust_env=False, follow_redirects=False
            ) as client:
                with client.stream(
                    "GET", jobs.url + "/jobs/" + job_id + "/download"
                ) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes():
                        if len(data) + len(chunk) > MAX_GLB_BYTES:
                            raise HTTPException(
                                413, "Generated GLB exceeds download limit"
                            )
                        data.extend(chunk)
        except httpx.HTTPError as exc:
            raise HTTPException(503, "3D output unavailable") from exc
        if (
            len(data) < 12
            or data[:4] != b"glTF"
            or int.from_bytes(data[4:8], "little") != 2
            or int.from_bytes(data[8:12], "little") != len(data)
        ):
            raise HTTPException(503, "Worker returned an invalid GLB")
        return Response(
            bytes(data),
            media_type="model/gltf-binary",
            headers={
                "Content-Disposition": f'attachment; filename="{job_id}.glb"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router
