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
    assert set(ConnectorRegistry.keys()) == set(BUILTIN_CONNECTOR_IDS)

    registry = create_builtin_capability_registry()
    for connector_id in BUILTIN_CONNECTOR_IDS:
        assert registry.contains(f"connector:{connector_id}:read")


def test_connector_default_requirement_is_concrete_read_capability():
    ensure_connectors_populated()
    gmail_cls = ConnectorRegistry.get("gmail")
    assert gmail_cls.capability_requirements() == ("connector:gmail:read",)


def test_digest_collect_resolves_exact_selected_connectors():
    policy = CapabilityPolicy()
    tool = DigestCollectTool()

    assert policy.resolve_effective_tool_capabilities(
        tool,
        {"sources": ["gmail", "gcalendar"]},
    ) == (
        "connector:gmail:read",
        "connector:gcalendar:read",
    )
