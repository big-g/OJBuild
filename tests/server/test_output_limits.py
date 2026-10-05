import asyncio
import json
from unittest.mock import MagicMock

import httpx
import pytest

from openjarvis.agents.orchestrator import OrchestratorAgent
from openjarvis.agents.simple import SimpleAgent
from openjarvis.core.types import Message, Role
from openjarvis.engine.ollama import OllamaEngine
from openjarvis.server.models import ChatCompletionRequest
from openjarvis.server.routes import _handle_agent, _handle_agent_stream


def generation(text="part", reason="length"):
    return dict(
        content=text,
        finish_reason=reason,
        usage=dict(prompt_tokens=2, completion_tokens=3, total_tokens=5),
    )


def request():
    return ChatCompletionRequest(
        model="selected",
        max_tokens=8192,
        messages=[{"role": "user", "content": "write a long poem"}],
    )


@pytest.mark.parametrize("agent_type", [SimpleAgent, OrchestratorAgent])
def test_budget_continuation_and_usage(agent_type):
    engine = MagicMock()
    engine.generate.side_effect = [
        generation("first"),
        generation("second"),
        generation("end", "stop"),
    ]
    agent = agent_type(engine, "original")
    original_tokens = agent._max_tokens
    response = _handle_agent(agent, "selected", request())
    assert response.choices[0].message.content == "firstsecondend"
    assert response.choices[0].finish_reason == "stop"
    assert response.usage.total_tokens == 15
    assert response.usage.prompt_tokens == 6
    assert all(
        call.kwargs["max_tokens"] == 8192 for call in engine.generate.call_args_list
    )
    assert all(
        call.kwargs["model"] == "selected" for call in engine.generate.call_args_list
    )
    assert agent._max_tokens == original_tokens and agent._model == "original"


def test_budget_restored_on_error():
    engine = MagicMock()
    engine.generate.side_effect = RuntimeError("failed")
    agent = SimpleAgent(engine, "original")
    tokens = agent._max_tokens
    with pytest.raises(RuntimeError):
        _handle_agent(agent, "selected", request())
    assert agent._max_tokens == tokens and agent._model == "original"
    assert not hasattr(agent, "_last_finish_reason")


def test_still_truncated_sse_reports_length():
    engine = MagicMock()
    engine.generate.side_effect = [generation()] * 3
    agent = SimpleAgent(engine, "original")

    async def consume():
        response = await _handle_agent_stream(agent, "selected", request())
        return "".join([chunk async for chunk in response.body_iterator])

    body = asyncio.run(consume())
    chunks = [
        json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: {")
    ]
    assert chunks[-1]["choices"][0]["finish_reason"] == "length"
    assert chunks[-1]["usage"]["completion_tokens"] == 9
    assert engine.generate.call_count == 3


def test_ollama_generate_preserves_done_reason():
    engine = OllamaEngine(host="http://testhost:11434")
    engine._client = httpx.Client(
        base_url="http://testhost:11434",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "message": {"content": "part"},
                    "done_reason": "length",
                    "eval_count": 3,
                },
            )
        )
    )
    try:
        result = engine.generate([Message(role=Role.USER, content="hi")], model="test")
        assert result["finish_reason"] == "length"
    finally:
        engine._client.close()
