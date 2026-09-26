"""Focused tests for canonical managed-agent tool resolution (#688)."""

from __future__ import annotations

from collections import Counter

import pytest

from openjarvis.agents import tool_resolver
from openjarvis.connectors.store import KnowledgeStore
from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools import description_loader
from openjarvis.tools._stubs import BaseTool, ToolSpec


class _AlphaTool(BaseTool):
    tool_id = "alpha"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="alpha", description="Alpha test tool")

    def execute(self, **params) -> ToolResult:
        return ToolResult(tool_name="alpha", content="alpha", success=True)


class _BetaTool(BaseTool):
    tool_id = "beta"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="beta", description="Beta test tool")

    def execute(self, **params) -> ToolResult:
        return ToolResult(tool_name="beta", content="beta", success=True)


class _NativeSharedTool(BaseTool):
    tool_id = "shared"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="shared", description="Native shared tool")

    def execute(self, **params) -> ToolResult:
        return ToolResult(tool_name="shared", content="native", success=True)


class _MCPSharedTool(BaseTool):
    tool_id = "shared"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="shared", description="MCP name collision")

    def execute(self, **params) -> ToolResult:
        return ToolResult(tool_name="shared", content="mcp", success=True)


class _MCPOnlyTool(BaseTool):
    tool_id = "mcp_only"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="mcp_only", description="MCP-only test tool")

    def execute(self, **params) -> ToolResult:
        return ToolResult(tool_name="mcp_only", content="mcp-only", success=True)


@pytest.fixture(autouse=True)
def _use_explicit_test_registrations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep these unit tests independent of import-time registry population."""

    monkeypatch.setattr(tool_resolver, "ensure_registries_populated", lambda: None)


def test_deep_research_grants_are_live_deduplicated_and_use_selected_model(
    tmp_path,
) -> None:
    """Agent-type grants must beat duplicate bare configured tools."""

    db_path = tmp_path / "knowledge.db"
    with KnowledgeStore(db_path=db_path) as store:
        store.store(
            "The RESOLVER_SENTINEL decision was approved.",
            source="test",
            doc_type="note",
        )

    engine = object()
    resolved = tool_resolver.resolve_agent_tools(
        {
            "agent_type": "deep_research",
            "config": {
                # Both names are already supplied by the agent-type grant.
                "tools": ["knowledge_search", "think", "think"],
            },
        },
        engine=engine,
        model="agent-selected-model",
        knowledge_db_path=db_path,
    )

    try:
        names = [tool.spec.name for tool in resolved.instances]
        assert set(names) == {
            "knowledge_search",
            "knowledge_sql",
            "scan_chunks",
            "think",
        }
        assert all(count == 1 for count in Counter(names).values())

        search = resolved.by_name["knowledge_search"]
        result = search.execute(query="RESOLVER_SENTINEL")
        assert result.success is True
        assert "RESOLVER_SENTINEL" in result.content

        scan = resolved.by_name["scan_chunks"]
        assert scan._engine is engine
        assert scan._model == "agent-selected-model"
    finally:
        # All three knowledge tools share this store connection.
        resolved.by_name["knowledge_sql"]._store.close()


@pytest.mark.parametrize(
    "tool_config",
    [
        ["alpha", "beta", "alpha"],
        " alpha, beta, alpha ",
    ],
)
def test_configured_tools_normalize_lists_and_comma_separated_strings(
    tool_config,
) -> None:
    ToolRegistry.register_value("alpha", _AlphaTool)
    ToolRegistry.register_value("beta", _BetaTool)

    resolved = tool_resolver.resolve_agent_tools(
        {"agent_type": "simple", "config": {"tools": tool_config}},
        engine=object(),
        model="test-model",
    )

    assert [tool.spec.name for tool in resolved.instances] == ["alpha", "beta"]
    assert [spec["function"]["name"] for spec in resolved.openai_specs] == [
        "alpha",
        "beta",
    ]


def test_registered_tool_advertisement_matches_to_openai_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime description overrides must reach canonical advertisements."""

    ToolRegistry.register_value("alpha", _AlphaTool)
    monkeypatch.setattr(
        description_loader,
        "get_tool_description_override",
        lambda name: "Runtime alpha description" if name == "alpha" else None,
    )

    resolved = tool_resolver.resolve_agent_tools(
        {"agent_type": "simple", "config": {"tools": ["alpha"]}},
        engine=object(),
        model="test-model",
    )

    tool = resolved.by_name["alpha"]
    assert resolved.openai_specs == [tool.to_openai_function()]
    assert (
        resolved.openai_specs[0]["function"]["description"]
        == "Runtime alpha description"
    )


def test_explicit_config_schema_takes_priority_over_tool_advertisement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ToolRegistry.register_value("alpha", _AlphaTool)
    monkeypatch.setattr(
        description_loader,
        "get_tool_description_override",
        lambda name: "Runtime alpha description" if name == "alpha" else None,
    )
    custom_spec = {
        "type": "function",
        "function": {
            "name": "alpha",
            "description": "Agent-specific alpha description",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }

    resolved = tool_resolver.resolve_agent_tools(
        {"agent_type": "simple", "config": {"tools": [custom_spec]}},
        engine=object(),
        model="test-model",
    )

    assert resolved.openai_specs == [custom_spec]
    assert resolved.by_name["alpha"].to_openai_function() == custom_spec


def test_invalid_tool_advertisement_falls_back_to_tool_spec() -> None:
    class _InvalidAdvertisementTool(_AlphaTool):
        def to_openai_function(self) -> dict[str, object]:
            raise RuntimeError("broken advertisement")

    ToolRegistry.register_value("alpha", _InvalidAdvertisementTool)

    resolved = tool_resolver.resolve_agent_tools(
        {"agent_type": "simple", "config": {"tools": ["alpha"]}},
        engine=object(),
        model="test-model",
    )

    assert resolved.openai_specs == [
        {
            "type": "function",
            "function": {
                "name": "alpha",
                "description": "Alpha test tool",
                "parameters": {},
            },
        }
    ]


def test_mcp_tools_merge_after_native_tools_without_name_collisions() -> None:
    ToolRegistry.register_value("shared", _NativeSharedTool)
    mcp_shared = _MCPSharedTool()
    mcp_only = _MCPOnlyTool()
    client = object()

    resolved = tool_resolver.resolve_agent_tools(
        {
            "agent_type": "simple",
            "config": {"tools": ["shared", "shared"]},
        },
        engine=object(),
        model="test-model",
        mcp_tools=[mcp_shared, mcp_only, mcp_only],
        mcp_clients=[client],
    )

    assert [tool.spec.name for tool in resolved.instances] == ["shared", "mcp_only"]
    assert isinstance(resolved.by_name["shared"], _NativeSharedTool)
    assert resolved.by_name["mcp_only"] is mcp_only
    assert resolved.mcp_clients == [client]
    assert [spec["function"]["name"] for spec in resolved.openai_specs] == [
        "shared",
        "mcp_only",
    ]


def test_mcp_tools_can_be_disabled_per_agent() -> None:
    ToolRegistry.register_value("shared", _NativeSharedTool)

    class _MustNotIterate:
        def __iter__(self):
            raise AssertionError("MCP tools must not be inspected after opt-out")

    resolved = tool_resolver.resolve_agent_tools(
        {
            "agent_type": "simple",
            "config": {"tools": ["shared"], "mcp_tools": False},
        },
        engine=object(),
        model="test-model",
        mcp_tools=_MustNotIterate(),
        mcp_clients=_MustNotIterate(),
    )

    assert [tool.spec.name for tool in resolved.instances] == ["shared"]
    assert resolved.mcp_clients == []


def test_resolved_agent_tools_are_registered_for_management() -> None:
    from openjarvis.security.capability_registry import (
        ResourceStatus,
        create_builtin_capability_registry,
    )
    from openjarvis.security.tool_management_registry import ToolManagementRegistry

    class _ManagedAlphaTool(BaseTool):
        tool_id = "managed_alpha"

        @property
        def spec(self) -> ToolSpec:
            return ToolSpec(
                name=self.tool_id,
                description="Managed alpha",
                required_capabilities=["none"],
            )

        def execute(self, **params) -> ToolResult:
            return ToolResult(tool_name=self.tool_id, content="ok", success=True)

    ToolRegistry.register_value("managed_alpha", _ManagedAlphaTool)
    managed = ToolManagementRegistry()
    capability_registry = create_builtin_capability_registry()

    resolved = tool_resolver.resolve_agent_tools(
        {
            "id": "agent-1",
            "agent_type": "simple",
            "config": {"tools": ["managed_alpha"]},
        },
        engine=object(),
        model="test-model",
        tool_management_registry=managed,
        capability_registry=capability_registry,
    )

    record = managed.require("builtin:managed_alpha")
    assert record.status == ResourceStatus.VALIDATED
    assert resolved.by_name["managed_alpha"].spec.name == "managed_alpha"


def test_agent_specific_schema_gets_distinct_management_identity() -> None:
    from openjarvis.security.capability_registry import (
        create_builtin_capability_registry,
    )
    from openjarvis.security.tool_management_registry import ToolManagementRegistry

    class _ConfiguredTool(BaseTool):
        tool_id = "configured"

        @property
        def spec(self) -> ToolSpec:
            return ToolSpec(
                name=self.tool_id,
                description="Configured base",
                required_capabilities=["none"],
            )

        def execute(self, **params) -> ToolResult:
            return ToolResult(tool_name=self.tool_id, content="ok", success=True)

    ToolRegistry.register_value("configured", _ConfiguredTool)
    managed = ToolManagementRegistry()

    custom_spec = {
        "type": "function",
        "function": {
            "name": "configured",
            "description": "Agent-specific schema",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        },
    }

    resolved = tool_resolver.resolve_agent_tools(
        {
            "id": "agent-42",
            "agent_type": "simple",
            "config": {"tools": [custom_spec]},
        },
        engine=object(),
        model="test-model",
        tool_management_registry=managed,
        capability_registry=create_builtin_capability_registry(),
    )

    configured = resolved.by_name["configured"]
    assert configured.management_identity == "agent:agent-42:configured"
    assert managed.contains("agent:agent-42:configured")
    assert not managed.contains("builtin:configured")


def test_configured_web_crawl_resolves_as_a_managed_agent_tool() -> None:
    from openjarvis.tools.scrapy_crawl import ScrapyCrawlTool

    ToolRegistry.register_value("web_crawl", ScrapyCrawlTool)
    resolved = tool_resolver.resolve_agent_tools(
        {
            "id": "research-agent",
            "agent_type": "simple",
            "config": {"tools": ["web_crawl"]},
        },
        engine=object(),
        model="test-model",
    )

    assert [tool.spec.name for tool in resolved.instances] == ["web_crawl"]
    assert set(resolved.openai_specs[0]["function"]["parameters"]["required"]) == {
        "url"
    }
