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
    return TestClient(create_app(engine, "vision-local", agent=agent, config=config)), engine


def _post(client, payload):
    with patch("openjarvis.server.routes.get_authenticated_user_id", return_value="test-user"):
        return client.post("/v1/chat/completions", json=payload)


def test_image_reaches_engine_without_agent():
    agent = MagicMock()
    client, engine = _client(agent)
    response = _post(client, {
        "model": "vision-local",
        "messages": [{"role": "user", "content": "What is shown?", "images": [PNG]}],
    })
    assert response.status_code == 200
    assert not agent.run.called
    messages = engine.generate.call_args.args[0]
    assert messages[-1].images == [PNG]


def test_ollama_message_conversion_keeps_images():
    messages = _to_messages([ChatMessage(role="user", content="Look", images=[PNG])])
    assert _to_openai_msgs(messages) == [
        {"role": "user", "content": "Look", "images": [PNG]}
    ]


def test_streaming_image_reaches_local_router():
    seen = []

    async def local_stream(messages, *, model, temperature, max_tokens):
        seen.extend(messages)
        yield "A picture"

    client, engine = _client()
    engine.stream = local_stream
    response = _post(client, {
        "model": "vision-local", "stream": True,
        "messages": [{"role": "user", "content": "Look", "images": [PNG]}],
    })
    assert response.status_code == 200
    assert seen[-1].images == [PNG]
    assert "A picture" in response.text


def test_invalid_image_and_cloud_route_rejected():
    client, engine = _client()
    for model, image in (("vision-local", "not-base64"), ("gpt-4o", PNG)):
        response = _post(client, {
            "model": model,
            "messages": [{"role": "user", "content": "Look", "images": [image]}],
        })
        assert response.status_code == 400
    assert not engine.generate.called
