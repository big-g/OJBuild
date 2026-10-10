"""Only a revalidated revision may reach chat clients."""

import json
from unittest.mock import MagicMock

import pytest

from openjarvis.agents._stubs import AgentResult
from openjarvis.agents.orchestrator import OrchestratorAgent
from openjarvis.core.config import JarvisConfig
from openjarvis.core.events import EventBus
from openjarvis.core.types import ToolResult
from openjarvis.server.app import create_app
from openjarvis.tools.web_search import WebSearchTool
from tests.server.helpers import authenticated_client


@pytest.mark.parametrize("stream", [False, True])
def test_chat_emits_only_revalidated_answer(stream):
    def verdict(supported):
        return {
            "content": json.dumps(
                {
                    "supported": supported,
                    "unsupported_claims": [] if supported else ["Unsupported sunshine"],
                    "reason": "Checked forecast",
                }
            )
        }

    engine = MagicMock()
    engine.engine_id = "mock"
    engine.health.return_value = True
    engine.list_models.return_value = ["test-model"]
    corrected = "Tomorrow's high will be 82°F."
    engine.generate.side_effect = [
        verdict(False),
        {"content": corrected},
        verdict(True),
    ]
    agent = OrchestratorAgent(
        engine,
        "test-model",
        tools=[WebSearchTool(api_key="test")],
    )
    agent._run_function_calling = MagicMock(
        return_value=AgentResult(
            content="Tomorrow's high will be 82°F and it will be sunny.",
            tool_results=[
                ToolResult(
                    tool_name="web_search",
                    success=True,
                    content="Tomorrow's high will be 82°F.",
                    metadata={
                        "evidence": {
                            "records": [
                                {
                                    "content": "Tomorrow's high will be 82°F.",
                                    "url": "https://weather.example.test/forecast",
                                }
                            ]
                        }
                    },
                )
            ],
        )
    )
    cfg = JarvisConfig()
    cfg.analytics.enabled = False
    cfg.traces.enabled = False
    app = create_app(engine, "test-model", agent=agent, bus=EventBus(), config=cfg)
    response = authenticated_client(app).post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "Weather forecast tomorrow?"}],
            "stream": stream,
        },
    )
    assert response.status_code == 200
    assert "it will be sunny" not in response.text
    assert "I couldn't verify" not in response.text
    if stream:
        chunks = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        content = "".join(
            (chunk["choices"][0]["delta"].get("content") or "")
            for chunk in chunks
            if chunk.get("choices")
        )
        assert content == corrected
    else:
        assert response.json()["choices"][0]["message"]["content"] == corrected
    assert engine.generate.call_count == 3
