import fcntl
import os

import pytest

from openjarvis.engine._base import EngineConnectionError
from openjarvis.engine.gpu_gate import gpu_guard, gpu_reservation, gpu_stream_guard


def test_shared_inference_blocks_exclusive_generation_and_releases(
    tmp_path, monkeypatch
):
    path = tmp_path / "gpu.lock"
    path.touch()
    monkeypatch.setenv("OPENJARVIS_GPU_LOCK_PATH", str(path))
    fd = os.open(path, os.O_RDWR)
    try:
        with gpu_reservation():
            with gpu_reservation():
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(EngineConnectionError):
            with gpu_reservation():
                pytest.fail("Inference entered during generation")
    finally:
        os.close(fd)


@pytest.mark.asyncio
async def test_async_guard_reserves_until_stream_close(tmp_path, monkeypatch):
    path = tmp_path / "gpu.lock"
    path.touch()
    monkeypatch.setenv("OPENJARVIS_GPU_LOCK_PATH", str(path))

    @gpu_stream_guard
    async def stream():
        yield "one"
        yield "two"

    iterator = stream()
    assert await anext(iterator) == "one"
    fd = os.open(path, os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        await iterator.aclose()
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_unconfigured_is_noop_but_missing_configured_lock_fails_closed(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("OPENJARVIS_GPU_LOCK_PATH", raising=False)

    @gpu_guard
    def request():
        return 42

    assert request() == 42
    monkeypatch.setenv("OPENJARVIS_GPU_LOCK_PATH", str(tmp_path / "absent"))
    with pytest.raises(EngineConnectionError):
        request()


def test_background_chat_redirects_and_blocks_extra_models(tmp_path, monkeypatch):
    import json

    from openjarvis.engine.gpu_gate import background_options, gpu_chat_guard

    path = tmp_path / "gpu.lock"
    path.touch()
    path.with_name(path.name + ".json").write_text(
        json.dumps({"background_model": "qwen3.5:2b"})
    )
    monkeypatch.setenv("OPENJARVIS_GPU_LOCK_PATH", str(path))

    @gpu_chat_guard
    def chat(*, model, max_tokens=4096, **kwargs):
        return model, max_tokens, kwargs, background_options()

    result = chat(model="qwen3.5:9b", num_ctx=32768, max_tokens=8192)
    assert result == (
        "qwen3.5:2b",
        1024,
        {"num_ctx": 2048, "think": False},
        {"num_ctx": 2048},
    )
    assert background_options() == {}

    @gpu_guard
    def embed():
        pytest.fail("Embedding must not load another model")

    with pytest.raises(EngineConnectionError):
        embed()
    path.with_name(path.name + ".json").unlink()
    assert chat(model="qwen3.5:9b")[0] == "qwen3.5:9b"


@pytest.mark.asyncio
async def test_stream_redirect_and_no_context_leak(tmp_path, monkeypatch):
    import json

    from openjarvis.engine.gpu_gate import background_options

    path = tmp_path / "gpu.lock"
    path.touch()
    path.with_name(path.name + ".json").write_text(
        json.dumps({"background_model": "qwen3.5:2b"})
    )
    monkeypatch.setenv("OPENJARVIS_GPU_LOCK_PATH", str(path))

    @gpu_stream_guard
    async def stream(model):
        yield model, background_options()
        yield model, background_options()

    iterator = stream("qwen3.5:9b")
    assert await anext(iterator) == (
        "qwen3.5:2b",
        {"num_ctx": 2048},
    )
    assert background_options() == {}
    await iterator.aclose()


def test_remote_ollama_keeps_its_model_and_optional_embeddings_fall_back(
    tmp_path, monkeypatch
):
    import json

    from openjarvis.engine.gpu_gate import gpu_chat_guard, gpu_optional_guard

    path = tmp_path / "gpu.lock"
    path.touch()
    Path = type(path)
    Path(str(path) + ".json").write_text(json.dumps({"background_model": "qwen3.5:2b"}))
    monkeypatch.setenv("OPENJARVIS_GPU_LOCK_PATH", str(path))

    class Remote:
        _host = "http://remote.example:11434"

        @gpu_chat_guard
        def chat(self, model):
            return model

    assert Remote().chat("remote:8b") == "remote:8b"

    @gpu_optional_guard
    def embed():
        pytest.fail("Must not load an extra model")

    assert embed() is None


def test_ollama_payload_uses_alternate_and_bounded_context(tmp_path, monkeypatch):
    import json

    import httpx

    from openjarvis.core.types import Message, Role
    from openjarvis.engine.ollama import OllamaEngine

    path = tmp_path / "gpu.lock"
    path.touch()
    path.with_name(path.name + ".json").write_text(
        json.dumps({"background_model": "qwen3.5:2b"})
    )
    monkeypatch.setenv("OPENJARVIS_GPU_LOCK_PATH", str(path))

    def respond(request):
        payload = json.loads(request.content)
        assert payload["model"] == "qwen3.5:2b"
        assert payload["options"]["num_ctx"] == 2048
        assert payload["options"]["num_predict"] == 512
        assert payload["think"] is False
        return httpx.Response(
            200,
            json={
                "model": "qwen3.5:2b",
                "message": {"role": "assistant", "content": "ok"},
            },
        )

    engine = OllamaEngine(trust_env=False)
    engine._client.close()
    engine._client = httpx.Client(
        base_url="http://localhost:11434", transport=httpx.MockTransport(respond)
    )
    try:
        result = engine.generate(
            [Message(role=Role.USER, content="hello")],
            model="qwen3.5:9b",
            max_tokens=512,
            num_ctx=32768,
            think=True,
        )
        assert result["content"] == "ok"
    finally:
        engine.close()


def test_unknown_image_worker_state_blocks_local_but_not_remote_chat(
    tmp_path, monkeypatch
):
    path = tmp_path / "gpu.lock"
    path.touch()
    path.with_name("gpu.lock.blocked").touch()
    monkeypatch.setenv("OPENJARVIS_GPU_LOCK_PATH", str(path))
    with pytest.raises(EngineConnectionError, match="recovery"):
        with gpu_reservation("http://127.0.0.1:11434"):
            pytest.fail("Blocked worker admitted chat")
    with gpu_reservation("http://192.168.1.20:11434") as lease:
        assert lease is None
