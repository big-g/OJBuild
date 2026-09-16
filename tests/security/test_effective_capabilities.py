"""Tests for effective tool capability resolution."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openjarvis.security.capabilities import (
    CapabilityPolicy,
    CapabilityResolutionError,
)


def _spec(name: str, capabilities=None):
    return SimpleNamespace(
        name=name,
        required_capabilities=[] if capabilities is None else capabilities,
    )


def test_explicit_tool_spec_capabilities_are_authoritative():
    policy = CapabilityPolicy(default_deny=True)

    assert policy.resolve_tool_capabilities(
        _spec("file_read", ["file:read"])
    ) == ("file:read",)


def test_legacy_mapping_is_only_fallback_when_declaration_is_empty():
    policy = CapabilityPolicy(default_deny=True)

    assert policy.resolve_tool_capabilities(_spec("file_read")) == ("file:read",)


def test_unknown_explicit_capability_fails_closed():
    policy = CapabilityPolicy(default_deny=True)

    with pytest.raises(CapabilityResolutionError, match="unknown capability"):
        policy.resolve_tool_capabilities(_spec("example", ["unknown:capability"]))


def test_unknown_tool_without_mapping_fails_closed():
    policy = CapabilityPolicy(default_deny=True)

    with pytest.raises(CapabilityResolutionError, match="no resolvable"):
        policy.resolve_tool_capabilities(_spec("unclassified_tool"))


def test_invalid_capability_declaration_fails_closed():
    policy = CapabilityPolicy(default_deny=True)

    with pytest.raises(CapabilityResolutionError, match="invalid required_capabilities"):
        policy.resolve_tool_capabilities(_spec("example", "file:read"))


def test_empty_capability_entry_fails_closed():
    policy = CapabilityPolicy(default_deny=True)

    with pytest.raises(CapabilityResolutionError, match="empty capability declaration"):
        policy.resolve_tool_capabilities(_spec("example", ["file:read", ""]))


def test_explicit_capability_glob_is_validated_against_registry():
    policy = CapabilityPolicy(default_deny=True)

    assert policy.resolve_tool_capabilities(_spec("file_tool", ["file:*"])) == ("file:*",)


def test_unknown_legacy_tool_mapping_fails_closed():
    policy = CapabilityPolicy(default_deny=True)

    with pytest.raises(CapabilityResolutionError):
        policy.resolve_tool_capabilities(_spec("not_in_legacy_mapping"))
