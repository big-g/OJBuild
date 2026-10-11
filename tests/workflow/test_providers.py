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


@pytest.mark.parametrize("memory_released", [True, False])
@respx.mock
def test_comfy_prompt_handoff_download_and_memory_release(
    monkeypatch, tmp_path, memory_released
):
    monkeypatch.setattr(
        "openjarvis.workflow.providers.image_gpu", lambda: nullcontext()
    )
    path = tmp_path / "gpu.lock"
    monkeypatch.setenv("OPENJARVIS_GPU_LOCK_PATH", str(path))
    monkeypatch.setattr("openjarvis.workflow.providers.time.sleep", lambda _: None)
    identity = str(uuid.uuid4())
    base = "http://127.0.0.1:8188"
    queue = respx.get(base + "/queue").mock(
        return_value=Response(200, json={"queue_running": [], "queue_pending": []})
    )
    samples = 0

    def stats_reply(_request):
        nonlocal samples
        samples += 1
        free = 14 if samples == 1 or (memory_released and samples >= 3) else 4
        return Response(
            200,
            json={
                "devices": [
                    {
                        "type": "cuda",
                        "index": 0,
                        "vram_free": free * 1024**3,
                    }
                ]
            },
        )

    stats = respx.get(base + "/system_stats").mock(side_effect=stats_reply)
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
    release = respx.post(base + "/free").mock(return_value=Response(200))
    params = ImageParams(instance_id="local", seed=12).model_dump()
    instance = SimpleNamespace(url=base, checkpoint="test.safetensors")
    if memory_released:
        output = generate_image(instance, "robot", params)
        assert output.startswith(b"\x89PNG")
    else:
        with pytest.raises(ValueError, match="did not release GPU memory"):
            generate_image(instance, "robot", params)
    import json

    payload = json.loads(submit.calls[0].request.content)
    assert payload["prompt"]["2"]["inputs"]["text"] == "robot"
    assert payload["prompt"]["5"]["inputs"]["seed"] == 12
    assert release.call_count == 1
    assert queue.call_count == stats.call_count == (3 if memory_released else 31)
    assert path.with_name("gpu.lock.blocked").exists() is (not memory_released)


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
    respx.get(base + "/system_stats").mock(
        return_value=Response(
            200,
            json={
                "devices": [{"type": "cuda", "index": 0, "vram_free": 14 * 1024**3}],
            },
        )
    )
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


@pytest.mark.parametrize("path", ["/free", "/interrupt", "/queue"])
@respx.mock
def test_comfy_control_endpoints_accept_empty_http_success(path):
    base = "http://127.0.0.1:8188"
    respx.post(base + path).mock(return_value=Response(200))
    assert request(base, path, payload={}, acknowledgement=True) is None


@respx.mock
def test_empty_response_does_not_substitute_for_required_json():
    base = "http://127.0.0.1:8188"
    respx.get(base + "/queue").mock(return_value=Response(200))
    with pytest.raises(ValueError, match="invalid JSON"):
        request(base, "/queue")


@respx.mock
def test_control_acknowledgement_does_not_hide_http_failure():
    import httpx

    base = "http://127.0.0.1:8188"
    respx.post(base + "/free").mock(return_value=Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        request(base, "/free", payload={}, acknowledgement=True)


@pytest.mark.parametrize("state", ["pending", "running"])
@respx.mock
def test_stop_own_job_accepts_empty_control_response(state, monkeypatch):
    from openjarvis.workflow.providers import stop_image_job

    base = "http://127.0.0.1:8188"
    respx.get(base + "/queue").mock(
        side_effect=[
            Response(200, json={"queue_" + state: [[0, "own"]]}),
            Response(200, json={"queue_running": [], "queue_pending": []}),
        ]
    )
    path = "/queue" if state == "pending" else "/interrupt"
    respx.post(base + path).mock(return_value=Response(200))
    assert stop_image_job(base, "own") is True
