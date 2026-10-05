import json

import httpx
import pytest

from openjarvis.core.types import Message
from openjarvis.engine.ollama import OllamaEngine
from openjarvis.server import cloud_router
from openjarvis.server.models import ChatCompletionRequest
from openjarvis.server.routes import _handle_stream


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["local", "openai", "anthropic", "google"])
async def test_direct_provider_stream_preserves_truncation(monkeypatch, provider):
    if provider == "local":
        lines = [
            json.dumps({"message": {"content": "part"}}),
            json.dumps({"done": True, "done_reason": "length"}),
        ]
        function = cloud_router.stream_local
    elif provider == "openai":
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        lines = [
            "data: " + json.dumps({"choices": [{"delta": {"content": "part"}}]}),
            "data: "
            + json.dumps({"choices": [{"delta": {}, "finish_reason": "length"}]}),
        ]
        function = cloud_router._stream_openai
    elif provider == "anthropic":
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        lines = [
            "data: "
            + json.dumps({"type": "content_block_delta", "delta": {"text": "part"}}),
            "data: "
            + json.dumps(
                {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}}
            ),
        ]
        function = cloud_router._stream_anthropic
    else:
        monkeypatch.setenv("GEMINI_API_KEY", "test")
        lines = [
            "data: "
            + json.dumps(
                {
                    "candidates": [
                        {
                            "content": {"parts": [{"text": "part"}]},
                            "finishReason": "MAX_TOKENS",
                        }
                    ]
                }
            )
        ]
        function = cloud_router._stream_google
    original = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="\n".join(lines))
    )
    monkeypatch.setattr(
        cloud_router.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=transport, **kwargs),
    )
    outcome = {}
    tokens = [
        token
        async for token in function(
            "model", [Message(role="user", content="hi")], 0.7, 2048, outcome=outcome
        )
    ]
    assert tokens == ["part"]
    assert outcome["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_no_agent_ollama_stream_propagates_finish_reason():
    engine = OllamaEngine(host="http://testhost:11434")
    data = (
        json.dumps({"message": {"content": "part"}})
        + "\n"
        + json.dumps({"done": True, "done_reason": "length", "eval_count": 3})
        + "\n"
    )
    engine._async_transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text=data)
    )
    try:
        response = await _handle_stream(
            engine,
            "model",
            ChatCompletionRequest(
                model="model", messages=[{"role": "user", "content": "hi"}], stream=True
            ),
        )
        body = "".join([part async for part in response.body_iterator])
        chunks = [
            json.loads(line[6:])
            for line in body.splitlines()
            if line.startswith("data: {")
        ]
        assert chunks[-1]["choices"][0]["finish_reason"] == "length"
    finally:
        if engine._async_client is not None:
            await engine._async_client.aclose()
        engine._client.close()
