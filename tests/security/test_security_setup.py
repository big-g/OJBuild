"""Tests for security setup fail-closed behavior."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import openjarvis.security as security


def _config(*, capabilities_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        security=SimpleNamespace(
            enabled=True,
            secret_scanner=False,
            pii_scanner=False,
            mode="redact",
            scan_input=False,
            scan_output=False,
            capabilities=SimpleNamespace(
                enabled=capabilities_enabled,
                policy_path=None,
            ),
            audit_log_path=":memory:",
        )
    )


def test_capability_setup_constructs_policy_when_enabled(monkeypatch):
    class FakePolicy:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(
        "openjarvis.security.capabilities.CapabilityPolicy",
        FakePolicy,
    )

    context = security.setup_security(_config(), engine=object())

    assert isinstance(context.capability_policy, FakePolicy)
    assert context.capability_registry is not None
    assert context.capability_policy.kwargs["registry"] is context.capability_registry


def test_capability_setup_fails_closed_when_policy_initialization_fails(monkeypatch):
    class FailingPolicy:
        def __init__(self, **kwargs):
            raise ValueError("policy initialization failed")

    monkeypatch.setattr(
        "openjarvis.security.capabilities.CapabilityPolicy",
        FailingPolicy,
    )

    with pytest.raises(RuntimeError, match="Capability security is enabled"):
        security.setup_security(_config(), engine=object())


def test_capability_setup_is_optional_when_disabled():
    context = security.setup_security(_config(capabilities_enabled=False), engine=object())

    assert context.capability_policy is None
    assert context.capability_registry is None
