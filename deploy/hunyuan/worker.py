"""Local, single-job Hunyuan worker. GPU memory is released by child exit."""

import fcntl
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Literal
from urllib.request import Request as URLRequest
from urllib.request import urlopen

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel, Field

ROOT = Path(os.environ.get("HUNYUAN_DATA", "/mnt/ai/hunyuan3d"))
REPO = Path(os.environ["HUNYUAN_REPO"])
JOBS = ROOT / "jobs"


def write_status(folder, status):
    temporary = folder / "status.tmp"
    temporary.write_text(json.dumps(status, indent=2))
    temporary.replace(folder / "status.json")


def generate(folder):
    import numpy as np
    import torch
    import trimesh
    from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline
    from hy3dgen.shapegen.models.autoencoders import SurfaceExtractors

    state = json.loads((folder / "status.json").read_text())
    variant = state["model"]
    subfolder = "hunyuan3d-dit-v2-0" + ("-turbo" if variant == "turbo" else "")
    start = time.monotonic()
    pipeline = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
        str(ROOT / "models/Hunyuan3D-2"),
        subfolder=subfolder,
        variant="fp16",
        use_safetensors=True,
        device="cuda",
        dtype=torch.float16,
    )
    pipeline.vae.surface_extractor = SurfaceExtractors["mc"]()
    torch.cuda.reset_peak_memory_stats()
    mesh = pipeline(
        image=str(folder / "input.png"),
        num_inference_steps=5 if variant == "turbo" else 30,
        guidance_scale=5.0,
        octree_resolution=256,
        num_chunks=4096,
        generator=torch.Generator(device="cuda").manual_seed(state["seed"]),
    )[0]
    if mesh is None or not len(mesh.faces) or not np.isfinite(mesh.vertices).all():
        raise RuntimeError("Generation returned an empty or invalid mesh")
    output = ROOT / "outputs" / (state["job_id"] + ".glb")
    mesh.export(str(output))
    checked = trimesh.load(str(output), force="mesh")
    if not len(checked.faces) or not np.isfinite(checked.vertices).all():
        raise RuntimeError("Exported mesh failed validation")
    torch.cuda.synchronize()
    state.update(
        status="completed",
        output=str(output),
        vertices=len(checked.vertices),
        faces=len(checked.faces),
        watertight=bool(checked.is_watertight),
        elapsed_seconds=round(time.monotonic() - start, 1),
        peak_allocated_vram_gib=round(torch.cuda.max_memory_allocated() / 1024**3, 2),
    )
    write_status(folder, state)


if __name__ == "__main__":
    generate(Path(sys.argv[1]))
    raise SystemExit(0)


app = FastAPI(title="OpenJarvis Hunyuan3D Worker", version="1.0")
busy = threading.Lock()
JOBS.mkdir(parents=True, exist_ok=True)
for file in JOBS.glob("*/status.json"):
    state = json.loads(file.read_text())
    if state["status"] == "running":
        state.update(status="failed", error="Worker restarted before job finished")
        write_status(file.parent, state)


@app.on_event("startup")
def clear_stale_lease():
    """The service kills child processes on restart; clear its old routing lease."""
    path = os.environ["HUNYUAN_GPU_LOCK_PATH"]
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Active inference prevented worker startup"
                    ) from None
                time.sleep(0.1)
        Path(path + ".json").unlink(missing_ok=True)
    finally:
        os.close(fd)


class Request(BaseModel):
    image_path: str
    model: Literal["standard", "turbo"] = "turbo"
    seed: int = Field(default=42, ge=0, le=2147483647)
    primary_model: str = Field(default="", max_length=2048)
    background_model: str = Field(default="", max_length=2048)


def folder_for(job_id):
    try:
        if str(uuid.UUID(job_id)) != job_id:
            raise ValueError()
    except ValueError:
        raise HTTPException(404, "Unknown job")
    folder = JOBS / job_id
    if not (folder / "status.json").is_file():
        raise HTTPException(404, "Unknown job")
    return folder


def ollama(path, payload=None):
    request = URLRequest(
        "http://127.0.0.1:11434" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def choose_background(models, primary, configured, variant):
    """Conservative budget; validate allocations on the target GPU."""
    capacity = float(os.environ.get("HUNYUAN_GPU_VRAM_GIB", "0"))
    reserve = 8.5 if variant == "turbo" else 11.5

    # 25% weight overhead plus 2 GiB for a 2048-token KV cache/runtime.
    def fits(model):
        size = model.get("size", 0) / 1024**3
        return size > 0 and reserve + size * 1.25 + 2 <= capacity

    if configured:
        selected = next((m for m in models if m["name"] == configured), None)
        if selected is None:
            raise HTTPException(
                400, "Configured generation chat model is not installed in local Ollama"
            )
        if not fits(selected):
            raise HTTPException(
                400,
                "Configured generation chat model exceeds the VRAM budget; "
                "use Turbo or configure a smaller model",
            )
        return selected["name"]
    family = primary.split(":")[0]
    original = next((m for m in models if m["name"] == primary), None)
    candidates = [
        m
        for m in models
        if family
        and m["name"].split(":")[0] == family
        and m["name"] != primary
        and fits(m)
        and (original is None or m.get("size", 0) < original.get("size", 0))
    ]
    return min(candidates, key=lambda m: m["size"])["name"] if candidates else None


def run_job(folder, gpu_fd):
    try:
        with (folder / "generation.log").open("w") as log:
            result = subprocess.run(
                [sys.executable, "-u", __file__, str(folder)],
                cwd=REPO,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=1800,
            )
        if result.returncode:
            raise RuntimeError("Generation failed; inspect generation.log")
    except Exception as error:
        state = json.loads((folder / "status.json").read_text())
        state.update(status="failed", error=str(error))
        write_status(folder, state)
    finally:
        # Wait for an ongoing alternate-model reply before restoring normal
        # routing. Generation has exited and released its GPU allocations.
        try:
            fcntl.flock(gpu_fd, fcntl.LOCK_EX)
            alternate = json.loads((folder / "status.json").read_text()).get(
                "background_model"
            )
            if alternate:
                try:
                    ollama(
                        "/api/generate",
                        {"model": alternate, "keep_alive": 0, "stream": False},
                    )
                except Exception:
                    pass  # Normal routing still restores if Ollama is offline.
            Path(os.environ["HUNYUAN_GPU_LOCK_PATH"] + ".json").unlink(missing_ok=True)
        finally:
            os.close(gpu_fd)
            busy.release()


@app.get("/health")
def health():
    return {"status": "ok", "busy": busy.locked(), "default_model": "turbo"}


@app.post("/jobs", status_code=202)
def submit(request: Request):
    image = Path(request.image_path).expanduser().resolve()
    allowed = ((ROOT / "inputs").resolve(), (REPO / "assets").resolve())
    if not any(image.is_relative_to(root) for root in allowed):
        raise HTTPException(
            400, "Image must be under the inputs or bundled assets directory"
        )
    if not image.is_file() or image.stat().st_size > 20 * 1024**2:
        raise HTTPException(400, "Missing image or image exceeds 20 MiB")
    try:
        with Image.open(image) as source:
            if source.width * source.height > 16_000_000:
                raise HTTPException(400, "Image exceeds 16 megapixels")
            normalized = source.convert("RGBA")
    except (OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(400, "Invalid PNG or JPEG image") from None
    if not busy.acquire(blocking=False):
        raise HTTPException(409, "A generation job is already running")
    folder = None
    gpu_fd = None
    published = False
    try:
        lock_path = os.environ["HUNYUAN_GPU_LOCK_PATH"]
        gpu_fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(gpu_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HTTPException(409, "An Ollama inference request is active")
        # All OpenJarvis inference paths hold the shared lock, so this exclusive
        # section cannot evict a model while an OpenJarvis request is running.
        try:
            loaded = ollama("/api/ps")["models"]
            installed = ollama("/api/tags")["models"]
        except Exception:
            raise HTTPException(503, "Cannot check Ollama models") from None
        primary = request.primary_model or (loaded[0]["name"] if loaded else "")
        alternate = choose_background(
            installed, primary, request.background_model, request.model
        )
        for model in loaded:
            ollama(
                "/api/generate",
                {"model": model["name"], "keep_alive": 0, "stream": False},
            )
        if ollama("/api/ps")["models"]:
            raise HTTPException(409, "Ollama models have not unloaded")
        lease_path = Path(lock_path + ".json")
        lease_path.unlink(missing_ok=True)
        if alternate:
            # Publish while exclusive; only the alternate may be loaded after
            # switching to a shared lock. The child still uses the Hunyuan GPU.
            temporary = lease_path.with_suffix(".tmp")
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "w") as output:
                json.dump({"background_model": alternate}, output)
            temporary.replace(lease_path)
            published = True
            fcntl.flock(gpu_fd, fcntl.LOCK_SH)
        job_id = str(uuid.uuid4())
        folder = JOBS / job_id
        folder.mkdir()
        normalized.save(folder / "input.png")
        state = {
            "job_id": job_id,
            "status": "running",
            "model": request.model,
            "seed": request.seed,
            "created_at": time.time(),
            "background_model": alternate,
            "chat_mode": "alternate" if alternate else "paused",
        }
        write_status(folder, state)
        threading.Thread(target=run_job, args=(folder, gpu_fd), daemon=True).start()
        return state
    except Exception:
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
        if gpu_fd is not None:
            # If submit failed after publishing the lease, wait for any request
            # already using it before removing it.
            if published:
                fcntl.flock(gpu_fd, fcntl.LOCK_EX)
                Path(os.environ["HUNYUAN_GPU_LOCK_PATH"] + ".json").unlink(
                    missing_ok=True
                )
            os.close(gpu_fd)
        busy.release()
        raise


@app.get("/jobs/{job_id}")
def status(job_id: str):
    return json.loads((folder_for(job_id) / "status.json").read_text())


@app.get("/jobs/{job_id}/download")
def download(job_id: str):
    state = status(job_id)
    if state["status"] != "completed":
        raise HTTPException(409, "Job is not completed")
    return FileResponse(
        state["output"], filename=job_id + ".glb", media_type="model/gltf-binary"
    )
