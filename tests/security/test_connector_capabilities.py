from __future__ import annotations

from openjarvis.connectors import ensure_connectors_populated
from openjarvis.core.registry import ConnectorRegistry
from openjarvis.security.capabilities import CapabilityPolicy
from openjarvis.security.capability_registry import (
    BUILTIN_CONNECTOR_IDS,
    create_builtin_capability_registry,
)
from openjarvis.tools.digest_collect import DigestCollectTool


def test_connector_inventory_matches_capability_vocabulary():
    ensure_connectors_populated()
    known_ids = set(BUILTIN_CONNECTOR_IDS)
    assert set(ConnectorRegistry.keys()) <= known_ids

    registry = create_builtin_capability_registry()
    for connector_id in known_ids:
        assert registry.contains(f"connector:{connector_id}:read")

    for connector_id, connector_cls in ConnectorRegistry.items():
        requirements = connector_cls.capability_requirements()
        assert f"connector:{connector_id}:read" in requirements
        assert all(registry.contains(cap) for cap in requirements)


def test_connector_default_requirement_is_concrete_read_capability():
    ensure_connectors_populated()
    obsidian_cls = ConnectorRegistry.get("obsidian")
    assert obsidian_cls.capability_requirements() == ("connector:obsidian:read",)


def test_digest_collect_resolves_exact_selected_connector():
    policy = CapabilityPolicy()
    tool = DigestCollectTool()

    assert policy.resolve_effective_tool_capabilities(
        tool,
        {"sources": ["obsidian"]},
    ) == ("connector:obsidian:read",)


def test_local_files_requires_file_access_as_well_as_connector_access():
    ensure_connectors_populated()
    policy = CapabilityPolicy()
    assert policy.resolve_effective_tool_capabilities(
        DigestCollectTool(), {"sources": ["local_files"]}
    ) == ("connector:local_files:read", "file:read")


def test_connector_grant_does_not_imply_access_to_local_files():
    from openjarvis.core.types import ToolCall
    from openjarvis.tools._stubs import ToolExecutor

    ensure_connectors_populated()
    policy = CapabilityPolicy(default_deny=True)
    policy.grant("reader", "connector:local_files:read")
    executor = ToolExecutor(
        [DigestCollectTool()], capability_policy=policy, agent_id="reader"
    )
    result = executor.execute(
        ToolCall(
            id="local", name="digest_collect", arguments='{"sources":["local_files"]}'
        )
    )
    assert not result.success
    assert "file:read" in result.content
