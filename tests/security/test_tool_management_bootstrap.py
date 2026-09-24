import importlib

from openjarvis.core.registry import ToolRegistry
from openjarvis.security.capability_registry import ResourceStatus
from openjarvis.security.tool_management_bootstrap import (
    build_builtin_tool_management_registry,
)


EXPECTED_UNRESOLVED: set[str] = set()


def test_all_static_registry_tools_are_managed():
    managed = build_builtin_tool_management_registry()

    assert len(tuple(managed.keys())) == len(ToolRegistry.keys())
    assert len(tuple(managed.keys())) == 48

    expected = {f"builtin:{name}" for name in ToolRegistry.keys()}
    assert set(managed.keys()) == expected


def test_specialized_tool_import_does_not_expand_builtin_inventory():
    import openjarvis.tools.knowledge_search as knowledge_search

    # Simulate the combined-suite condition that originally caused the
    # bootstrap count to become order-dependent.
    importlib.reload(knowledge_search)
    ToolRegistry.clear()

    managed = build_builtin_tool_management_registry()

    assert len(tuple(managed.keys())) == 48
    assert "builtin:knowledge_search" not in managed.keys()
    assert "knowledge_search" not in ToolRegistry.keys()


def test_builtin_identity_and_provenance_are_stable():
    managed = build_builtin_tool_management_registry()

    for identity, record in managed.items():
        assert identity == f"builtin:{record.execution_name}"
        assert record.provenance.source_type == "builtin"
        assert record.provenance.source_id == record.implementation_id
        assert record.implementation_id.startswith("openjarvis.tools.")
        assert record.fingerprint.algorithm == "sha256"
        assert record.fingerprint.value


def test_expected_tools_fail_capability_validation():
    managed = build_builtin_tool_management_registry()

    unresolved = {
        identity
        for identity, record in managed.items()
        if record.status == ResourceStatus.REGISTERED
    }

    assert unresolved == EXPECTED_UNRESOLVED

    for identity in unresolved:
        record = managed.require(identity)
        assert record.validation is not None
        assert not record.validation.passed
        assert record.approval is None

        failed_checks = {
            check.name
            for check in record.validation.checks
            if not check.passed
        }
        assert failed_checks == {"capability_resolution"}


def test_other_builtins_validate_but_are_not_approved():
    managed = build_builtin_tool_management_registry()

    validated = {
        identity
        for identity, record in managed.items()
        if record.status == ResourceStatus.VALIDATED
    }

    assert len(validated) == 48
    assert validated.isdisjoint(EXPECTED_UNRESOLVED)

    for identity in validated:
        record = managed.require(identity)
        assert record.validation is not None
        assert record.validation.passed
        assert record.approval is None
        assert not record.is_approved()


def test_no_builtin_is_implicitly_approved():
    managed = build_builtin_tool_management_registry()

    assert all(
        not record.is_approved()
        for _, record in managed.items()
    )
