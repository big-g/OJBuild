"""Tests for RBAC capabilities system (Phase 14.4)."""

from __future__ import annotations

import pytest

from openjarvis.core.types import ToolCall, ToolResult
from openjarvis.security.capabilities import (
    DEFAULT_TOOL_CAPABILITIES,
    Capability,
    CapabilityPolicy,
    CapabilityResolutionError,
)
from openjarvis.tools._stubs import BaseTool, ToolExecutor, ToolSpec
from openjarvis.tools.code_interpreter import CodeInterpreterTool
from openjarvis.tools.code_interpreter_docker import DockerCodeInterpreterTool
from openjarvis.tools.file_read import FileReadTool
from openjarvis.tools.repl import ReplTool

class TestCapability:
    def test_capability_values(self):
        assert Capability.FILE_READ == "file:read"
        assert Capability.NETWORK_FETCH == "network:fetch"
        assert Capability.CODE_EXECUTE == "code:execute"
        assert Capability.SYSTEM_ADMIN == "system:admin"

    def test_all_capabilities_exist(self):
        expected = {
            "file:read",
            "file:write",
            "network:fetch",
            "code:execute",
            "memory:read",
            "memory:write",
            "channel:send",
            "tool:invoke",
            "schedule:create",
            "system:admin",
        }
        actual = {c.value for c in Capability}
        assert expected == actual


class TestCapabilityPolicy:
    def test_default_allow(self):
        policy = CapabilityPolicy()
        assert policy.check("agent1", "file:read")
        assert policy.check("agent1", "code:execute")

    def test_default_deny(self):
        policy = CapabilityPolicy(default_deny=True)
        assert not policy.check("agent1", "file:read")

    def test_explicit_grant(self):
        policy = CapabilityPolicy(default_deny=True)
        policy.grant("agent1", "file:read")
        assert policy.check("agent1", "file:read")
        assert not policy.check("agent1", "code:execute")

    def test_explicit_deny(self):
        policy = CapabilityPolicy()
        policy.deny("agent1", "code:execute")
        assert not policy.check("agent1", "code:execute")
        assert policy.check("agent1", "file:read")

    def test_deny_overrides_grant(self):
        policy = CapabilityPolicy()
        policy.grant("agent1", "code:execute")
        policy.deny("agent1", "code:execute")
        assert not policy.check("agent1", "code:execute")

    def test_resource_pattern(self):
        policy = CapabilityPolicy(default_deny=True)
        policy.grant("agent1", "file:read", pattern="/safe/*")
        assert policy.check("agent1", "file:read", "/safe/data.txt")
        assert not policy.check("agent1", "file:read", "/etc/passwd")

    def test_glob_pattern(self):
        policy = CapabilityPolicy(default_deny=True)
        policy.grant("agent1", "file:*")
        assert policy.check("agent1", "file:read")
        assert policy.check("agent1", "file:write")
        assert not policy.check("agent1", "code:execute")

    def test_list_grants(self):
        policy = CapabilityPolicy()
        policy.grant("agent1", "file:read")
        policy.grant("agent1", "code:execute")
        grants = policy.list_grants("agent1")
        assert len(grants) == 2

    def test_list_agents(self):
        policy = CapabilityPolicy()
        policy.grant("agent1", "file:read")
        policy.grant("agent2", "code:execute")
        agents = policy.list_agents()
        assert set(agents) == {"agent1", "agent2"}

    def test_no_policy_agent(self):
        policy = CapabilityPolicy()
        assert policy.list_grants("unknown") == []

    def test_save_and_load(self, tmp_path):
        path = tmp_path / "policy.json"
        policy = CapabilityPolicy()
        policy.grant("agent1", "file:read")
        policy.deny("agent1", "code:execute")
        policy.save(path)

        loaded = CapabilityPolicy(policy_path=str(path))
        assert loaded.check("agent1", "file:read")
        assert not loaded.check("agent1", "code:execute")

    def test_load_nonexistent_file(self):
        policy = CapabilityPolicy(policy_path="/nonexistent/path.json")
        # Should not raise, just have no policies
        assert policy.check("agent1", "file:read")

    def test_default_tool_capabilities(self):
        assert "file:read" in DEFAULT_TOOL_CAPABILITIES.get("file_read", [])
        assert "network:fetch" in DEFAULT_TOOL_CAPABILITIES.get("web_search", [])
        assert "code:execute" in DEFAULT_TOOL_CAPABILITIES.get("code_interpreter", [])

    def test_explicit_none_capability(self):
        class ToolSpecStub:
            name = "calculator"
            required_capabilities = ["none"]

        policy = CapabilityPolicy()
        assert policy.resolve_tool_capabilities(ToolSpecStub()) == ()


    def test_none_cannot_be_combined_with_capabilities(self):
        class ToolSpecStub:
            name = "invalid"
            required_capabilities = ["none", "file:read"]

        policy = CapabilityPolicy()

        with pytest.raises(CapabilityResolutionError):
            policy.resolve_tool_capabilities(ToolSpecStub())


    def test_empty_capability_declaration_uses_legacy_mapping(self):
        class ToolSpecStub:
            name = "memory_store"
            required_capabilities = []

        policy = CapabilityPolicy()
        assert policy.resolve_tool_capabilities(ToolSpecStub()) == ("memory:write",)

class TestExplicitToolCapabilities:
    """First-batch tools must carry their capability on ToolSpec."""

    def test_code_interpreter(self):
        assert CodeInterpreterTool().spec.required_capabilities == ["code:execute"]

    def test_code_interpreter_docker(self):
        assert DockerCodeInterpreterTool().spec.required_capabilities == ["code:execute"]

    def test_repl(self):
        assert ReplTool().spec.required_capabilities == ["code:execute"]

    def test_file_read(self):
        assert FileReadTool().spec.required_capabilities == ["file:read"]

class _CapabilityTestTool(BaseTool):
    tool_id = "capability_test"

    def __init__(self, required_capabilities: list[str]) -> None:
        self._spec = ToolSpec(
            name="capability_test",
            description="Test tool for capability enforcement.",
            required_capabilities=required_capabilities,
        )
        self.executed = False

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    def execute(self, **params: object) -> ToolResult:
        self.executed = True
        return ToolResult(
            tool_name=self.tool_id,
            content="executed",
            success=True,
        )


class TestToolExecutorCapabilities:
    def test_explicit_none_allows_capability_free_tool(self) -> None:
        tool = _CapabilityTestTool(["none"])
        policy = CapabilityPolicy(default_deny=True)
        executor = ToolExecutor(
            [tool],
            capability_policy=policy,
            agent_id="test-agent",
        )

        result = executor.execute(
            ToolCall(
                id="test-none",
                name="capability_test",
                arguments="{}",
            )
        )

        assert result.success
        assert result.content == "executed"
        assert tool.executed

    def test_unknown_capability_blocks_execution(self) -> None:
        tool = _CapabilityTestTool(["not:a:real:capability"])
        policy = CapabilityPolicy()
        executor = ToolExecutor(
            [tool],
            capability_policy=policy,
            agent_id="test-agent",
        )

        result = executor.execute(
            ToolCall(
                id="test-invalid",
                name="capability_test",
                arguments="{}",
            )
        )

        assert not result.success
        assert "Capability resolution failed" in result.content
        assert not tool.executed

