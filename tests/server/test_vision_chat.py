"""Image input reaches a local vision engine and fails closed elsewhere."""

import base64
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from openjarvis.server.app import create_app
from openjarvis.server.cloud_router import _to_openai_msgs
from openjarvis.server.models import ChatMessage
from openjarvis.server.routes import _to_messages

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"sample").decode()


def _client(agent=None):
    from openjarvis.core.config import JarvisConfig

    engine = MagicMock()
    engine.engine_id = "ollama"
    engine.health.return_value = True
    engine.list_models.return_value = ["vision-local"]
    engine.generate.return_value = {"content": "A picture", "usage": {}}
    config = JarvisConfig()
    config.analytics.enabled = False
    config.traces.enabled = False
    return TestClient(
        create_app(engine, "vision-local", agent=agent, config=config)
    ), engine


def _post(client, payload):
    with patch(
        "openjarvis.server.routes.get_authenticated_user_id", return_value="test-user"
    ):
        return client.post("/v1/chat/completions", json=payload)


def test_image_reaches_engine_without_agent():
    agent = MagicMock()
    client, engine = _client(agent)
    response = _post(
        client,
        {
            "model": "vision-local",
            "messages": [
                {"role": "user", "content": "What is shown?", "images": [PNG, PNG]}
            ],
        },
    )
    assert response.status_code == 200
    assert not agent.run.called
    messages = engine.generate.call_args.args[0]
    assert messages[-1].images == [PNG, PNG]


def test_ollama_message_conversion_keeps_images():
    messages = _to_messages(
        [ChatMessage(role="user", content="Look", images=[PNG, PNG])]
    )
    assert _to_openai_msgs(messages) == [
        {"role": "user", "content": "Look", "images": [PNG, PNG]}
    ]


def test_streaming_image_reaches_local_router():
    seen = []

    async def local_stream(messages, *, model, temperature, max_tokens):
        seen.extend(messages)
        yield "A picture"

    client, engine = _client()
    engine.stream = local_stream
    response = _post(
        client,
        {
            "model": "vision-local",
            "stream": True,
            "messages": [{"role": "user", "content": "Look", "images": [PNG]}],
        },
    )
    assert response.status_code == 200
    assert seen[-1].images == [PNG]
    assert "A picture" in response.text


def test_invalid_image_and_cloud_route_rejected():
    client, engine = _client()
    for model, image in (("vision-local", "not-base64"), ("gpt-4o", PNG)):
        response = _post(
            client,
            {
                "model": model,
                "messages": [{"role": "user", "content": "Look", "images": [image]}],
            },
        )
        assert response.status_code == 400
    assert not engine.generate.called


def test_prior_image_is_rejected_before_chat_history_is_used():
    client, engine = _client()
    response = _post(
        client,
        {
            "model": "vision-local",
            "messages": [
                {"role": "user", "content": "Look", "images": [PNG]},
                {"role": "assistant", "content": "I see it."},
                {"role": "user", "content": "What color was it?"},
            ],
        },
    )
    assert response.status_code == 400
    assert "latest user message" in response.json()["detail"]
    assert not engine.generate.called


def test_five_images_are_rejected():
    client, engine = _client()
    response = _post(
        client,
        {
            "model": "vision-local",
            "messages": [{"role": "user", "content": "Look", "images": [PNG] * 5}],
        },
    )
    assert response.status_code == 400
    assert not engine.generate.called


def test_multimodal_orchestrator_keeps_image_through_tool_round_trip():
    from openjarvis.agents._stubs import AgentContext
    from openjarvis.agents.orchestrator import OrchestratorAgent
    from openjarvis.core.types import ToolResult
    from openjarvis.server.models import ChatCompletionRequest
    from openjarvis.server.routes import _handle_agent
    from openjarvis.tools._stubs import BaseTool, ToolSpec

    class InspectTool(BaseTool):
        tool_id = "inspect_test"

        @property
        def spec(self):
            return ToolSpec(
                name=self.tool_id,
                description="Inspect",
                parameters={"type": "object", "properties": {}},
                required_capabilities=["none"],
            )

        def execute(self, **params):
            return ToolResult(
                tool_name=self.tool_id, success=True, content="Inspection complete"
            )

    engine = MagicMock()
    seen = []

    def generate(messages, **kwargs):
        seen.append(list(messages))
        if len(seen) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {"id": "inspect-1", "name": "inspect_test", "arguments": "{}"}
                ],
            }
        return {"content": "Inspection complete", "usage": {}}

    engine.generate.side_effect = generate
    agent = OrchestratorAgent(engine, "vision-local", tools=[InspectTool()])
    req = ChatCompletionRequest(
        model="vision-local",
        messages=[
            ChatMessage(role="user", content="Inspect this object", images=[PNG])
        ],
    )
    response = _handle_agent(agent, "vision-local", req)
    assert response.choices[0].message.content == "Inspection complete"
    assert len(seen) == 2
    assert all(any(m.images == [PNG] for m in messages) for messages in seen)
    assert not any(m.images for m in agent._build_messages("Next turn", AgentContext()))


def test_image_http_request_uses_multimodal_agent():
    from openjarvis.agents.orchestrator import OrchestratorAgent

    agent_engine = MagicMock()
    agent_engine.generate.return_value = {"content": "A mug", "usage": {}}
    agent = OrchestratorAgent(agent_engine, "vision-local")
    client, direct_engine = _client(agent)
    response = _post(
        client,
        {
            "model": "vision-local",
            "messages": [
                {"role": "user", "content": "Describe the object", "images": [PNG]}
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "A mug"
    assert not direct_engine.generate.called
    assert any(m.images == [PNG] for m in agent_engine.generate.call_args.args[0])
