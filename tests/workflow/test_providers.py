"""Bounded local generation protocol; no live models or untrusted graphs run."""

import io
import uuid
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
import respx
from httpx import Response

from openjarvis.workflow.jobs import ImageParams
from openjarvis.workflow.providers import (
    generate_image,
    image_gpu,
    image_graph,
    request,
)

Image = pytest.importorskip("PIL.Image")


def png():
    out = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(out, "PNG")
    return out.getvalue()


@respx.mock
def test_comfy_prompt_handoff_download_and_memory_release(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "openjarvis.workflow.providers.image_gpu", lambda: nullcontext()
    )
    path = tmp_path / "gpu.lock"
    monkeypatch.setenv("OPENJARVIS_GPU_LOCK_PATH", str(path))
    identity = str(uuid.uuid4())
    base = "http://127.0.0.1:8188"
    queue = respx.get(base + "/queue").mock(
        return_value=Response(200, json={"queue_running": [], "queue_pending": []})
    )
    submit = respx.post(base + "/prompt").mock(
        return_value=Response(200, json={"prompt_id": identity})
    )
    respx.get(base + "/history/" + identity).mock(
        return_value=Response(
            200,
            json={
                identity: {
                    "status": {"status_str": "success"},
                    "outputs": {
                        "7": {
                            "images": [
                                {
                                    "filename": "image.png",
                                    "subfolder": "",
                                    "type": "output",
                                }
                            ]
                        }
                    },
                }
            },
        )
    )
    respx.get(base + "/view").mock(return_value=Response(200, content=png()))
    release = respx.post(base + "/free").mock(return_value=Response(200, json={}))
    params = ImageParams(instance_id="local", seed=12).model_dump()
    output = generate_image(
        SimpleNamespace(url=base, checkpoint="test.safetensors"), "robot", params
    )
    assert output.startswith(b"\x89PNG")
    import json

    payload = json.loads(submit.calls[0].request.content)
    assert payload["prompt"]["2"]["inputs"]["text"] == "robot"
    assert payload["prompt"]["5"]["inputs"]["seed"] == 12
    assert queue.call_count == 1 and release.call_count == 1
    assert not path.with_name("gpu.lock.blocked").exists()


@respx.mock
def test_local_provider_does_not_follow_redirects():
    respx.get("http://127.0.0.1:8188/test").mock(
        return_value=Response(302, headers={"location": "https://example.com"})
    )
    with pytest.raises(Exception):
        request("http://127.0.0.1:8188", "/test")


def test_fixed_graph_does_not_interpret_prompt_as_nodes():
    graph = image_graph(
        "test.safetensors",
        '{"class_type":"arbitrary"}',
        ImageParams(instance_id="local").model_dump(),
    )
    assert len(graph) == 7
    assert graph["2"]["inputs"]["text"] == '{"class_type":"arbitrary"}'
    assert "arbitrary" not in [node["class_type"] for node in graph.values()]


def test_shared_gpu_lock_blocks_image_job_when_inference_is_active(
    tmp_path, monkeypatch
):
    import fcntl
    import os

    path = tmp_path / "gpu.lock"
    path.touch()
    monkeypatch.setenv("OPENJARVIS_GPU_LOCK_PATH", str(path))
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        with pytest.raises(ValueError, match="GPU busy"):
            with image_gpu():
                pytest.fail("Image generation must not evict an active inference")
    finally:
        os.close(fd)


def test_blocked_gpu_denies_new_image_work(tmp_path, monkeypatch):
    path = tmp_path / "gpu.lock"
    path.touch()
    path.with_name("gpu.lock.blocked").touch()
    monkeypatch.setenv("OPENJARVIS_GPU_LOCK_PATH", str(path))
    with pytest.raises(ValueError, match="recovery"):
        with image_gpu():
            pytest.fail("Blocked image worker admitted")


@respx.mock
def test_lost_submission_blocks_shared_gpu(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setattr(
        "openjarvis.workflow.providers.image_gpu", lambda: nullcontext()
    )
    path = tmp_path / "gpu.lock"
    monkeypatch.setenv("OPENJARVIS_GPU_LOCK_PATH", str(path))
    base = "http://127.0.0.1:8188"
    respx.get(base + "/queue").mock(return_value=Response(200, json={}))
    respx.post(base + "/prompt").mock(side_effect=httpx.ReadTimeout("lost reply"))
    with pytest.raises(ValueError, match="submission state is unknown"):
        generate_image(
            SimpleNamespace(url=base, checkpoint="test.safetensors"),
            "robot",
            ImageParams(instance_id="local").model_dump(),
        )
    assert path.with_name("gpu.lock.blocked").is_file()


@respx.mock
def test_cancellation_never_interrupts_someone_elses_running_job():
    from openjarvis.workflow.providers import stop_image_job

    base = "http://127.0.0.1:8188"
    respx.get(base + "/queue").mock(
        return_value=Response(
            200,
            json={
                "queue_running": [[0, "own"], [1, "someone-else"]],
                "queue_pending": [],
            },
        )
    )
    interrupt = respx.post(base + "/interrupt").mock(return_value=Response(200))
    assert stop_image_job(base, "own") is False
    assert interrupt.call_count == 0
