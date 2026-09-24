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
        assert connector_cls.capability_requirements() == (
            f"connector:{connector_id}:read",
        )


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
