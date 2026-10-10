"""Exercise memory policy without loading model weights or a GPU."""

import importlib.util
from pathlib import Path

import pytest
from fastapi import HTTPException


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setenv("HUNYUAN_DATA", str(tmp_path))
    monkeypatch.setenv("HUNYUAN_REPO", str(tmp_path))
    monkeypatch.setenv("HUNYUAN_GPU_VRAM_GIB", "15.92")
    path = Path(__file__).parents[2] / "deploy/hunyuan/worker.py"
    spec = importlib.util.spec_from_file_location("hunyuan_test_worker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_automatic_family_budget_and_explicit_configuration(worker):
    models = [
        {"name": "qwen3.5:9b", "size": 6 * 1024**3},
        {"name": "qwen3.5:2b", "size": int(2.5 * 1024**3)},
        {"name": "other:1b", "size": 1024**3},
    ]
    assert worker.choose_background(models, "qwen3.5:9b", "", "turbo") == "qwen3.5:2b"
    assert worker.choose_background(models, "qwen3.5:9b", "", "standard") is None
    assert (
        worker.choose_background(models, "qwen3.5:9b", "other:1b", "standard")
        == "other:1b"
    )
    for configured in ("missing:2b", "qwen3.5:9b"):
        with pytest.raises(HTTPException) as exc:
            worker.choose_background(models, "qwen3.5:9b", configured, "turbo")
        assert exc.value.status_code == 400
    assert worker.choose_background(models, "other:1b", "", "turbo") is None


def test_active_inference_rejects_without_unloading(worker, tmp_path, monkeypatch):
    import fcntl
    import os

    from PIL import Image

    inputs = tmp_path / "inputs"
    inputs.mkdir()
    image = inputs / "image.png"
    Image.new("RGB", (2, 2)).save(image)
    path = tmp_path / "gpu.lock"
    path.touch()
    monkeypatch.setenv("HUNYUAN_GPU_LOCK_PATH", str(path))
    fd = os.open(path, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_SH)
    monkeypatch.setattr(
        worker, "ollama", lambda *args: pytest.fail("Must not unload active model")
    )
    try:
        with pytest.raises(HTTPException) as exc:
            worker.submit(worker.Request(image_path=str(image)))
        assert exc.value.status_code == 409
        assert not worker.busy.locked()
    finally:
        os.close(fd)


def test_worker_publishes_alternate_during_child_and_restores_on_exit(
    worker, tmp_path, monkeypatch
):
    import json

    from PIL import Image

    from openjarvis.engine.gpu_gate import gpu_chat_guard

    inputs = tmp_path / "inputs"
    inputs.mkdir()
    image = inputs / "image.png"
    Image.new("RGB", (2, 2)).save(image)
    path = tmp_path / "gpu.lock"
    path.touch()
    monkeypatch.setenv("HUNYUAN_GPU_LOCK_PATH", str(path))
    monkeypatch.setenv("OPENJARVIS_GPU_LOCK_PATH", str(path))
    requests = []

    def ollama(endpoint, payload=None):
        requests.append((endpoint, payload))
        if endpoint == "/api/tags":
            return {
                "models": [
                    {"name": "qwen3.5:2b", "size": 2 * 1024**3},
                    {"name": "qwen3.5:9b", "size": 6 * 1024**3},
                ]
            }
        if endpoint == "/api/ps":
            return (
                {"models": [{"name": "qwen3.5:9b"}]}
                if len(requests) == 1
                else {"models": []}
            )
        return {}

    monkeypatch.setattr(worker, "ollama", ollama)

    @gpu_chat_guard
    def chat(model):
        return model

    def child(*args, **kwargs):
        assert chat("qwen3.5:9b") == "qwen3.5:2b"
        lease = json.loads(Path(str(path) + ".json").read_text())
        assert lease["background_model"] == "qwen3.5:2b"
        folder = Path(args[0][-1])
        state = json.loads((folder / "status.json").read_text())
        state["status"] = "completed"
        worker.write_status(folder, state)

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr(worker.subprocess, "run", child)

    class Thread:
        def __init__(self, target, args, **kwargs):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(worker.threading, "Thread", Thread)
    result = worker.submit(
        worker.Request(image_path=str(image), primary_model="qwen3.5:9b")
    )
    assert result["background_model"] == "qwen3.5:2b"
    assert not worker.busy.locked()
    assert not Path(str(path) + ".json").exists()
    assert chat("qwen3.5:9b") == "qwen3.5:9b"
