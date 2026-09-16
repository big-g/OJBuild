"""Tests for authoritative capability vocabulary and policy validation."""

from __future__ import annotations

import pytest

from openjarvis.security.capabilities import CapabilityPolicy
from openjarvis.security.capability_registry import (
    BUILTIN_CAPABILITIES,
    create_builtin_capability_registry,
)


def test_builtin_registry_contains_canonical_vocabulary():
    registry = create_builtin_capability_registry()
    assert set(registry.keys()) == {item[0] for item in BUILTIN_CAPABILITIES}


def test_policy_rejects_unknown_capability_grant():
    policy = CapabilityPolicy(default_deny=True)
    with pytest.raises(KeyError, match="Unknown capability pattern"):
        policy.grant("agent1", "unknown:capability")


def test_policy_rejects_unknown_capability_deny():
    policy = CapabilityPolicy(default_deny=True)
    with pytest.raises(KeyError, match="Unknown capability pattern"):
        policy.deny("agent1", "unknown:capability")


def test_policy_rejects_unknown_capability_at_check():
    policy = CapabilityPolicy()
    assert not policy.check("agent1", "unknown:capability")


def test_policy_accepts_known_capability_glob():
    policy = CapabilityPolicy(default_deny=True)
    policy.grant("agent1", "file:*")
    assert policy.check("agent1", "file:read")
    assert policy.check("agent1", "file:write")
    assert not policy.check("agent1", "code:execute")
