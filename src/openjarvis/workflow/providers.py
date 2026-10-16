"""Bounded local service adapters and fixed mesh subprocess transport."""

from __future__ import annotations

import fcntl
import io
import json
import logging
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
from openjarvis.workflow.errors import GenerationError

logger = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024**2


def request(
    url,
    path,
    *,
    payload=None,
    params=None,
    binary=False,
    acknowledgement=False,
    max_bytes=MAX_BYTES,
):
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
                if len(data) + len(chunk) > (max_bytes if binary else 2 * 1024**2):
                    raise ValueError("Local generation server returned oversized data")
                data.extend(chunk)
    if binary:
        return bytes(data)
    if acknowledgement:
        return None
    try:
        value = json.loads(data)
    except ValueError:
        raise ValueError("Local generation server returned invalid JSON") from None
    if not isinstance(value, dict):
        raise ValueError("Local generation server must return a JSON object")
    return value


@contextmanager
def image_gpu():
    """Use the existing exclusive GPU lock, pausing local Ollama during image jobs."""
    path = os.environ.get("OPENJARVIS_GPU_LOCK_PATH")
    if not path:
        raise ValueError(
            "Configure the shared GPU lock before enabling local image generation"
        )
    if Path(path + ".blocked").exists():
        raise GenerationError(
            "Local GPU recovery is in progress after an image job failure; "
            "retry shortly",
            "gpu_recovery_required",
            "The backend recovery monitor will check the worker. If recovery "
            "does not complete, inspect the GPU recovery entries in API logs.",
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
        for attempt in range(15):
            models = request(ollama, "/api/ps").get("models")
            if not isinstance(models, list):
                raise ValueError("Invalid Ollama model statistics during GPU handoff")
            logger.info("GPU handoff: Ollama loaded model count=%s", len(models))
            if not models:
                break
            time.sleep(1)
        else:
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


def block_gpu(url=None, baseline=None):
    path = os.environ.get("OPENJARVIS_GPU_LOCK_PATH")
    if path:
        fd = os.open(
            path + ".blocked",
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(fd, "w") as output:
            output.write(
                json.dumps(
                    {
                        "owner": "openjarvis-image-v1",
                        "url": url,
                        "baseline": baseline,
                        "created": time.time(),
                        "restarts": 0,
                    }
                )
            )
            output.flush()
            os.fsync(output.fileno())


def stop_image_job(url, identity):
    """Remove only this pending job; interrupt only if it is the sole active job."""
    queue = request(url, "/queue")
    running = queue.get("queue_running", [])
    pending = queue.get("queue_pending", [])
    if any(item[1] == identity for item in pending):
        request(url, "/queue", payload={"delete": [identity]}, acknowledgement=True)
    if any(item[1] == identity for item in running):
        if len(running) != 1:
            return False
        request(url, "/interrupt", payload={}, acknowledgement=True)
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


def gpu_memory(url):
    """Capture free VRAM and PyTorch reservations (ROCm uses type CUDA)."""
    devices = request(url, "/system_stats").get("devices")
    if not isinstance(devices, list):
        raise ValueError("Image server returned invalid GPU statistics")
    result = {}
    for device in devices:
        if not isinstance(device, dict) or device.get("type") != "cuda":
            continue
        index, free = device.get("index"), device.get("vram_free")
        reserved = device.get("torch_vram_total")
        if (
            type(index) is not int
            or index < 0
            or index in result
            or type(free) is not int
            or free < 0
            or type(reserved) is not int
            or reserved < 0
        ):
            raise ValueError("Image server returned invalid GPU statistics")
        result[index] = (free, reserved)
    if not result:
        raise ValueError("Image server must report an available CUDA/ROCm GPU")
    return result


def cleanup_snapshot(url, baseline):
    """Validate worker telemetry and report every release criterion."""
    queue = request(url, "/queue")
    if any(
        not isinstance(queue.get(key), list)
        for key in ("queue_running", "queue_pending")
    ):
        raise ValueError("Image server returned invalid queue statistics")
    current = gpu_memory(url)
    snapshot = {
        "running": len(queue["queue_running"]),
        "pending": len(queue["queue_pending"]),
        "baseline": baseline,
        "memory": current,
    }
    ready = (
        not snapshot["running"]
        and not snapshot["pending"]
        and all(
            index in current
            and current[index][0] >= free - 1024**3
            and current[index][1] <= 256 * 1024**2
            for index, (free, _) in baseline.items()
        )
    )
    return ready, snapshot


def wait_for_gpu_release(url, baseline):
    # Acknowledgement of /free is not completion. Keep the exclusive GPU lease
    # until the worker queue and allocator independently confirm release.
    snapshot = None
    for attempt in range(30):
        ready, snapshot = cleanup_snapshot(url, baseline)
        if ready:
            logger.info("GPU cleanup complete: %s", snapshot)
            return
        if attempt in (0, 10, 20):
            logger.info("GPU cleanup pending: %s", snapshot)
        time.sleep(1)
    logger.error("GPU cleanup timed out: %s", snapshot)
    raise ValueError(
        "Image server did not release GPU memory; recovery monitor will retry"
    )


def generate_image(instance, prompt, params):
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
        raise ValueError("Provide a text prompt of 1–4000 characters")
    with image_gpu():
        queue = request(instance.url, "/queue")
        if queue.get("queue_running") or queue.get("queue_pending"):
            raise ValueError("Image server is busy; finish its queued jobs and retry")
        baseline = gpu_memory(instance.url)
        # Persist the reservation before submission, so an API crash/restart
        # cannot release the GPU to chat while ComfyUI continues externally.
        graph = image_graph(instance.checkpoint, prompt, params)
        block_gpu(instance.url, baseline)
        safe_to_release = False
        try:
            response = request(
                instance.url,
                "/prompt",
                payload={
                    "prompt": graph,
                    "client_id": uuid.uuid4().hex,
                },
            )
            prompt_id = str(uuid.UUID(response["prompt_id"]))
        except httpx.HTTPStatusError as exc:
            # ComfyUI's validation response explicitly rejects the prompt.
            # Keep the reservation if health/idle verification fails.
            if exc.response.status_code == 400:
                try:
                    wait_for_gpu_release(instance.url, baseline)
                except Exception:
                    raise ValueError(
                        "Image job rejected and worker health is unverified; "
                        "administrator recovery is required"
                    ) from exc
                path = os.environ.get("OPENJARVIS_GPU_LOCK_PATH")
                if path:
                    Path(path + ".blocked").unlink(missing_ok=True)
                raise GenerationError(
                    "Image job rejected before execution. Check the selected "
                    "checkpoint and generation settings, then retry.",
                    "submission_rejected",
                    "Verify the checkpoint filename and generation parameters. "
                    "No GPU recovery is needed; the worker was verified idle.",
                ) from exc
            raise ValueError(
                "Image submission state is unknown; recover the worker"
            ) from exc
        except Exception:
            # Submission may have succeeded even if its acknowledgement was lost.
            # Keep the original recovery metadata after an uncertain submission.
            raise ValueError(
                "Image submission state is unknown; recover the worker"
            ) from None
        try:
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                history = request(instance.url, "/history/" + prompt_id).get(prompt_id)
                if history:
                    if history.get("status", {}).get("status_str") == "error":
                        raise GenerationError(
                            "Image server failed while executing the job.",
                            "execution_failed",
                            "Check the image worker log at this run's time for "
                            "the failing node, checkpoint or accelerator error.",
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
            logger.exception("Image execution failed before GPU cleanup")
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
                    acknowledgement=True,
                )
                wait_for_gpu_release(instance.url, baseline)
            except Exception as exc:
                safe_to_release = False
                raise GenerationError(
                    (str(exc) + ". " if isinstance(exc, ValueError) else "")
                    + "Image worker cleanup failed; GPU remains reserved.",
                    "gpu_recovery_required",
                    "The backend recovery monitor will retry cleanup and verify "
                    "worker health. Check GPU recovery diagnostics in API logs "
                    "if the reservation remains blocked.",
                ) from exc
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
            data = request(
                jobs.url,
                "/jobs/" + identity + "/download",
                binary=True,
                max_bytes=64 * 1024**2,
            )
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


def remove_background(data, params):
    """Run segmentation without sharing GPU state or downloading request-time models."""
    if len(data) > MAX_BYTES:
        raise ValueError("Image exceeds 20 MiB")
    with tempfile.TemporaryDirectory(prefix="openjarvis-foreground-") as folder:
        source, output = Path(folder) / "input", Path(folder) / "foreground.png"
        source.write_bytes(data)
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("background_worker.py")),
                str(source),
                str(output),
                json.dumps(params),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=dict(
                os.environ,
                OMP_NUM_THREADS="1",
                OPENBLAS_NUM_THREADS="1",
                NUMBA_NUM_THREADS="1",
            ),
        )
        try:
            stdout, _ = process.communicate(timeout=260)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise ValueError("Background removal timed out") from None
        try:
            report = json.loads(stdout)
        except ValueError:
            raise ValueError(
                "Background worker failed; check installed dependencies and memory"
            ) from None
        if process.returncode or "error" in report:
            raise ValueError(report.get("error", "Background removal failed"))
        if not output.is_file() or output.stat().st_size > MAX_BYTES:
            raise ValueError("Invalid foreground output")
        return output.read_bytes(), report
