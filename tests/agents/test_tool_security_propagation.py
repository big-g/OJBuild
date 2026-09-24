from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from openjarvis.agents.deep_research import DeepResearchAgent
from openjarvis.agents.monitor_operative import MonitorOperativeAgent
from openjarvis.agents.native_openhands import NativeOpenHandsAgent
from openjarvis.agents.native_react import NativeReActAgent
from openjarvis.agents.operative import OperativeAgent
from openjarvis.agents.orchestrator import OrchestratorAgent
from openjarvis.agents.rlm import RLMAgent
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec


class _ProbeTool(BaseTool):
    tool_id = "probe"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="probe",
            description="Security propagation probe.",
            required_capabilities=["none"],
        )

    def execute(self, **params) -> ToolResult:
        return ToolResult(tool_name="probe", content="ok", success=True)


@pytest.mark.parametrize(
    "agent_cls",
    [
        OrchestratorAgent,
        NativeReActAgent,
        NativeOpenHandsAgent,
        OperativeAgent,
        MonitorOperativeAgent,
        DeepResearchAgent,
        RLMAgent,
    ],
)
def test_tool_using_agent_keeps_security_dependencies(agent_cls):
    engine = MagicMock()
    engine.engine_id = "mock"
    policy = object()
    management = object()

    agent = agent_cls(
        engine,
        "test-model",
        tools=[_ProbeTool()],
        max_turns=1,
        capability_policy=policy,
        tool_management_registry=management,
    )

    assert agent._executor is not None
    assert agent._executor._capability_policy is policy
    assert agent._executor._tool_management_registry is management
