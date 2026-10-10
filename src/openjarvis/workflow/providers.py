"""Bounded local service adapters and fixed mesh subprocess transport."""

from __future__ import annotations

import fcntl
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx

from openjarvis.artifacts.store import _directory

MAX_BYTES = 20 * 1024**2


def request(url, path, *, payload=None, params=None, binary=False):
    data = bytearray()
    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
        with client.stream(
            "POST" if payload is not None else "GET",
            url + path,
            json=payload,
            params=params,
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                if len(data) + len(chunk) > (MAX_BYTES if binary else 2 * 1024**2):
                    raise ValueError("Local generation server returned oversized data")
                data.extend(chunk)
    return bytes(data) if binary else json.loads(data)


@contextmanager
def image_gpu():
    """Use the existing exclusive GPU lock, pausing local Ollama during image jobs."""
    path = os.environ.get("OPENJARVIS_GPU_LOCK_PATH")
    if not path:
        raise ValueError(
            "Configure the shared GPU lock before enabling local image generation"
        )
    if Path(path + ".blocked").exists():
        raise ValueError(
            "Local GPU requires administrator recovery after an image job failure"
        )
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "GPU busy; finish the active chat or generation and retry"
            ) from None
        if Path(path + ".blocked").exists():
            raise ValueError("Local GPU requires administrator recovery")
        ollama = "http://127.0.0.1:11434"
        for model in request(ollama, "/api/ps").get("models", []):
            request(
                ollama,
                "/api/generate",
                payload={
                    "model": model["name"],
                    "keep_alive": 0,
                    "stream": False,
                },
            )
        if request(ollama, "/api/ps").get("models"):
            raise ValueError("Ollama did not unload; retry image generation later")
        yield
    finally:
        os.close(fd)


def image_graph(checkpoint, prompt, params):
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["1", 1]},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": params["negative_prompt"], "clip": ["1", 1]},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width": params["width"],
                "height": params["height"],
                "batch_size": 1,
            },
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "seed": params["seed"],
                "steps": params["steps"],
                "cfg": 7.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
            },
        },
        "6": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
        },
        "7": {
            "class_type": "SaveImage",
            "inputs": {
                "images": ["6", 0],
                "filename_prefix": "openjarvis_" + uuid.uuid4().hex,
            },
        },
    }


def normalize_image(data):
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 16_000_000:
            raise ValueError("Generated image exceeds 16 megapixels")
        output = io.BytesIO()
        image.convert("RGBA").save(output, format="PNG")
        if output.tell() > MAX_BYTES:
            raise ValueError("Generated image exceeds 20 MiB")
        return output.getvalue()


def block_gpu():
    path = os.environ.get("OPENJARVIS_GPU_LOCK_PATH")
    if path:
        fd = os.open(path + ".blocked", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(
                "Image job active or interrupted; stop ComfyUI before recovery.\n"
            )
            output.flush()
            os.fsync(output.fileno())


def stop_image_job(url, identity):
    """Remove only this pending job; interrupt only if it is the sole active job."""
    queue = request(url, "/queue")
    running = queue.get("queue_running", [])
    pending = queue.get("queue_pending", [])
    if any(item[1] == identity for item in pending):
        request(url, "/queue", payload={"delete": [identity]})
    if any(item[1] == identity for item in running):
        if len(running) != 1:
            return False
        request(url, "/interrupt", payload={})
    for _ in range(20):
        queue = request(url, "/queue")
        if not any(
            item[1] == identity
            for item in (
                queue.get("queue_running", []) + queue.get("queue_pending", [])
            )
        ):
            return True
        time.sleep(1)
    return False


def generate_image(instance, prompt, params):
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
        raise ValueError("Provide a text prompt of 1–4000 characters")
    with image_gpu():
        queue = request(instance.url, "/queue")
        if queue.get("queue_running") or queue.get("queue_pending"):
            raise ValueError("Image server is busy; finish its queued jobs and retry")
        # Persist the reservation before submission, so an API crash/restart
        # cannot release the GPU to chat while ComfyUI continues externally.
        block_gpu()
        safe_to_release = False
        try:
            response = request(
                instance.url,
                "/prompt",
                payload={
                    "prompt": image_graph(instance.checkpoint, prompt, params),
                    "client_id": uuid.uuid4().hex,
                },
            )
            prompt_id = str(uuid.UUID(response["prompt_id"]))
        except Exception:
            # Submission may have succeeded even if its acknowledgement was lost.
            block_gpu()
            raise ValueError(
                "Image submission state is unknown; recover the worker"
            ) from None
        try:
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                history = request(instance.url, "/history/" + prompt_id).get(prompt_id)
                if history:
                    if history.get("status", {}).get("status_str") == "error":
                        raise ValueError(
                            "Image server rejected the job; check its log "
                            "and checkpoint"
                        )
                    images = history.get("outputs", {}).get("7", {}).get("images", [])
                    if images:
                        item = images[0]
                        if (
                            item.get("type") != "output"
                            or not isinstance(item.get("filename"), str)
                            or any(c in item["filename"] for c in "/\\")
                            or item.get("subfolder", "") != ""
                        ):
                            raise ValueError(
                                "Image server returned an invalid output reference"
                            )
                        output = normalize_image(
                            request(
                                instance.url,
                                "/view",
                                params={
                                    "filename": item["filename"],
                                    "subfolder": "",
                                    "type": "output",
                                },
                                binary=True,
                            )
                        )
                        safe_to_release = True
                        return output
                time.sleep(1)
            raise ValueError(
                "Image generation timed out; inspect the provider before retrying"
            )
        except Exception:
            try:
                stopped = stop_image_job(instance.url, prompt_id)
            except Exception:
                stopped = False
            safe_to_release = stopped
            raise
        finally:
            try:
                request(
                    instance.url,
                    "/free",
                    payload={
                        "unload_models": True,
                        "free_memory": True,
                    },
                )
            except Exception:
                safe_to_release = False
                raise
            finally:
                path = os.environ.get("OPENJARVIS_GPU_LOCK_PATH")
                if safe_to_release and path:
                    Path(path + ".blocked").unlink(missing_ok=True)


def generate_mesh(jobs, owner, data, params, config):
    data = normalize_image(data)
    with jobs.db() as db:
        if (
            db.execute("SELECT count(*) FROM jobs WHERE owner=?", (owner,)).fetchone()[
                0
            ]
            >= 100
        ):
            raise ValueError("3D job limit reached (100 per account)")
    name = str(uuid.uuid4()) + ".png"
    with _directory(jobs.inputs) as fd:
        handle = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
        )
        with os.fdopen(handle, "wb") as output:
            output.write(data)
    try:
        submitted = jobs.call(
            "/jobs",
            json={
                "image_path": str(jobs.inputs / name),
                "model": params["model"],
                "seed": params["seed"],
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
        identity = str(uuid.UUID(submitted["job_id"]))
        with jobs.db() as db:
            db.execute("INSERT INTO jobs VALUES(?,?)", (identity, owner))
    finally:
        with _directory(jobs.inputs) as fd:
            os.unlink(name, dir_fd=fd)
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        state = jobs.status(owner, identity)
        if state["status"] == "failed":
            raise ValueError("Hunyuan generation failed; inspect its worker log")
        if state["status"] == "completed":
            data = request(jobs.url, "/jobs/" + identity + "/download", binary=True)
            from openjarvis.workflow.mesh_worker import check_glb

            check_glb(data)
            return identity, data
        time.sleep(2)
    raise ValueError("Hunyuan job timed out; inspect the existing job before retrying")


def mesh_operation(filename, data, operation, params):
    kind = Path(filename).suffix.lower()
    if kind not in {".glb", ".stl", ".ply"}:
        raise ValueError("Select a GLB or STL mesh, or a workflow PLY intermediate")
    with tempfile.TemporaryDirectory(prefix="openjarvis-mesh-") as folder:
        source = Path(folder) / ("input" + kind)
        output = Path(folder) / (
            "output.stl" if operation == "mesh_export_stl" else "output.ply"
        )
        source.write_bytes(data)
        environment = dict(os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("mesh_worker.py")),
                str(source),
                str(output),
                operation,
                json.dumps(params),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=environment,
        )
        try:
            stdout, _ = process.communicate(timeout=260)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise ValueError("Mesh operation timed out") from None
        try:
            report = json.loads(stdout)
        except ValueError:
            raise ValueError(
                "Mesh worker failed; install generation dependencies "
                "and inspect server logs"
            ) from None
        if process.returncode or "error" in report:
            raise ValueError(report.get("error", "Mesh processing failed"))
        if output.exists() and output.stat().st_size > MAX_BYTES:
            raise ValueError("Mesh output exceeds 20 MiB")
        return output.read_bytes() if output.exists() else None, report
