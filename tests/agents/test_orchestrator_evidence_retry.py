"""A skipped retrieval gets one retry, without bypassing governance."""

from unittest.mock import MagicMock

import pytest

from openjarvis.agents.orchestrator import OrchestratorAgent
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec


class ForecastSource(BaseTool):
    tool_id = "forecast_source"

    def __init__(self):
        self.calls = 0

    @property
    def spec(self):
        return ToolSpec(
            name=self.tool_id,
            description="Current forecast source",
            evidence_kinds=["current"],
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        )

    def execute(self, **params):
        self.calls += 1
        return ToolResult(
            tool_name=self.tool_id,
            success=True,
            content="Today: Sunny. High 72 F.",
            metadata={
                "evidence": {
                    "records": [
                        {
                            "url": "https://forecast.example.test/today",
                            "content": "Today: Sunny. High 72 F.",
                        }
                    ]
                }
            },
        )


def response(mode, *, tool=False):
    if mode == "structured":
        content = (
            'TOOL: forecast_source\nINPUT: {"query":"forecast"}'
            if tool
            else "FINAL_ANSWER: Today is sunny with a high of 72 F."
        )
        return {"content": content, "finish_reason": "stop"}
    result = {"content": "Today is sunny with a high of 72 F.", "finish_reason": "stop"}
    if tool:
        result["tool_calls"] = [
            {
                "id": "forecast",
                "name": "forecast_source",
                "arguments": '{"query":"forecast"}',
            }
        ]
    return result


@pytest.mark.parametrize("mode", ["function_calling", "structured"])
def test_skipped_retrieval_retries_and_invokes_source(mode):
    source = ForecastSource()
    engine = MagicMock()
    engine.generate.side_effect = [
        response(mode),
        response(mode, tool=True),
        response(mode),
    ]
    agent = OrchestratorAgent(engine, "test", tools=[source], mode=mode)
    result = agent.run("What is the forecast for Kernersville NC?")
    assert result.metadata["evidence_status"] == "obtained"
    assert source.calls == 1 and engine.generate.call_count == 3
    messages = engine.generate.call_args_list[1].args[0]
    assert any("requires retrieved evidence" in (m.content or "") for m in messages)


@pytest.mark.parametrize("mode", ["function_calling", "structured"])
def test_model_ignoring_retry_remains_blocked(mode):
    source = ForecastSource()
    engine = MagicMock()
    engine.generate.return_value = response(mode)
    result = OrchestratorAgent(engine, "test", tools=[source], mode=mode).run(
        "What is the forecast for Kernersville NC?"
    )
    assert result.content == "I couldn't retrieve the required data."
    assert source.calls == 0 and engine.generate.call_count == 2


@pytest.mark.parametrize("mode", ["function_calling", "structured"])
def test_retry_preserves_governance_denial(mode):
    source = ForecastSource()
    engine = MagicMock()
    engine.generate.side_effect = [
        response(mode),
        response(mode, tool=True),
        response(mode),
    ]
    agent = OrchestratorAgent(
        engine,
        "test",
        tools=[source],
        mode=mode,
        before_tool_call=lambda name, args: False,
    )
    result = agent.run("What is the forecast for Kernersville NC?")
    assert source.calls == 0
    assert not result.tool_results[0].success
    assert result.content == "I couldn't retrieve the required data."


def test_ordinary_question_does_not_add_a_retry():
    engine = MagicMock()
    engine.generate.return_value = response("function_calling")
    OrchestratorAgent(engine, "test", tools=[ForecastSource()]).run(
        "Explain triangles."
    )
    assert engine.generate.call_count == 1


def test_retry_respects_max_turns():
    engine = MagicMock()
    engine.generate.return_value = response("function_calling")
    result = OrchestratorAgent(
        engine,
        "test",
        tools=[ForecastSource()],
        max_turns=1,
    ).run("What is the forecast for Kernersville NC?")
    assert engine.generate.call_count == 1
    assert result.content == "I couldn't retrieve the required data."
