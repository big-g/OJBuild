from datetime import datetime, timezone

import pytest

from openjarvis.security.capability_registry import (
    ApprovalRecord,
    Provenance,
    ResourceStatus,
    ValidationCheck,
    ValidationRecord,
)
from openjarvis.security.tool_management_registry import (
    ManagedToolRecord,
    ToolManagementRegistry,
    compute_tool_fingerprint,
)
from openjarvis.tools._stubs import ToolSpec


def make_record(name: str = "test_tool") -> ManagedToolRecord:
    spec = ToolSpec(
        name=name,
        description="Test tool",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
        required_capabilities=["file:read"],
    )
    provenance = Provenance(
        source_type="builtin",
        source_id="tests.test_tool_management_registry",
    )
    fingerprint = compute_tool_fingerprint(
        identity=f"builtin:{name}",
        spec=spec,
        provenance=provenance,
        implementation_id=f"tests.{name}",
        is_local=True,
    )
    return ManagedToolRecord(
        identity=f"builtin:{name}",
        spec=spec,
        provenance=provenance,
        fingerprint=fingerprint,
        implementation_id=f"tests.{name}",
        is_local=True,
    )


def make_validation(record: ManagedToolRecord) -> ValidationRecord:
    return ValidationRecord(
        validation_id="validation-1",
        validator="test-validator",
        validator_version="1",
        timestamp=datetime.now(timezone.utc),
        fingerprint=record.fingerprint,
        checks=(
            ValidationCheck(
                name="test",
                passed=True,
                severity="error",
            ),
        ),
    )


def make_approval(
    record: ManagedToolRecord,
    validation: ValidationRecord,
) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id="approval-1",
        approved_by="test-admin",
        timestamp=datetime.now(timezone.utc),
        validation_id=validation.validation_id,
        fingerprint=record.fingerprint,
    )


def test_registration_does_not_authorize():
    registry = ToolManagementRegistry()
    record = registry.register(make_record())

    assert record.status == ResourceStatus.REGISTERED
    assert not record.is_approved()


def test_duplicate_registration_is_rejected():
    registry = ToolManagementRegistry()
    registry.register(make_record())

    with pytest.raises(ValueError, match="already registered"):
        registry.register(make_record())


def test_validation_then_approval():
    registry = ToolManagementRegistry()
    record = registry.register(make_record())

    validation = make_validation(record)
    registry.validate(record.identity, validation)

    assert record.status == ResourceStatus.VALIDATED
    assert not record.is_approved()

    approval = make_approval(record, validation)
    registry.approve(record.identity, approval)

    assert record.status == ResourceStatus.APPROVED
    assert record.is_approved()


def test_validation_fingerprint_mismatch_is_rejected():
    registry = ToolManagementRegistry()
    record = registry.register(make_record())

    other = make_record("other_tool")
    validation = make_validation(other)

    with pytest.raises(ValueError, match="fingerprint mismatch"):
        registry.validate(record.identity, validation)


def test_disable_and_reenable_preserve_valid_approval():
    registry = ToolManagementRegistry()
    record = registry.register(make_record())

    validation = make_validation(record)
    registry.validate(record.identity, validation)
    registry.approve(
        record.identity,
        make_approval(record, validation),
    )

    registry.disable(record.identity)
    assert record.status == ResourceStatus.DISABLED
    assert not record.is_approved()

    registry.reenable(record.identity)
    assert record.status == ResourceStatus.APPROVED
    assert record.is_approved()


def test_changed_fingerprint_invalidates_approval():
    registry = ToolManagementRegistry()
    record = registry.register(make_record())

    validation = make_validation(record)
    registry.validate(record.identity, validation)
    registry.approve(
        record.identity,
        make_approval(record, validation),
    )
    assert record.is_approved()

    changed_spec = ToolSpec(
        name="test_tool",
        description="Changed description is irrelevant",
        parameters={
            "type": "object",
            "properties": {
                "value": {"type": "string"},
                "dangerous": {"type": "boolean"},
            },
        },
        required_capabilities=["file:read"],
    )

    changed_fingerprint = compute_tool_fingerprint(
        identity=record.identity,
        spec=changed_spec,
        provenance=record.provenance,
        implementation_id=record.implementation_id,
        is_local=record.is_local,
    )

    registry.update_definition(
        record.identity,
        spec=changed_spec,
        provenance=record.provenance,
        fingerprint=changed_fingerprint,
        implementation_id=record.implementation_id,
        is_local=record.is_local,
    )

    assert record.status == ResourceStatus.REGISTERED
    assert record.validation is None
    assert record.approval is None
    assert not record.is_approved()


def test_description_only_change_does_not_change_fingerprint():
    record = make_record()

    changed_spec = ToolSpec(
        name=record.spec.name,
        description="Completely different human-readable description",
        parameters=record.spec.parameters,
        required_capabilities=record.spec.required_capabilities,
    )

    fingerprint = compute_tool_fingerprint(
        identity=record.identity,
        spec=changed_spec,
        provenance=record.provenance,
        implementation_id=record.implementation_id,
        is_local=record.is_local,
    )

    assert fingerprint == record.fingerprint


def _approved_record():
    registry = ToolManagementRegistry()
    record = registry.register(make_record())
    validation = make_validation(record)
    registry.validate(record.identity, validation)
    registry.approve(record.identity, make_approval(record, validation))
    return registry, record


@pytest.mark.parametrize(
    "field,value",
    [
        ("required_capabilities", ["system:admin"]),
        ("requires_confirmation", True),
        ("timeout_seconds", 100.0),
        ("evidence_kinds", ["current"]),
        (
            "parameters",
            {"type": "object", "properties": {"command": {"type": "string"}}},
        ),
        ("metadata", {"trust": "changed"}),
    ],
)
def test_mutated_spec_cannot_retain_executable_approval(field, value):
    registry, record = _approved_record()
    setattr(record.spec, field, value)
    assert not record.is_approved()
    assert not registry.check_execution(record.identity)[0]
    assert not registry.check_execution(
        record.identity, fingerprint=record.fingerprint
    )[0]


def test_nested_mutation_is_detected_without_update_definition():
    registry, record = _approved_record()
    record.spec.parameters["properties"]["value"]["type"] = "integer"
    assert not record.is_approved()
    assert not registry.check_execution(record.identity)[0]


@pytest.mark.parametrize(
    "field,value",
    [
        ("implementation_id", "changed.implementation"),
        ("is_local", False),
        ("provenance", Provenance(source_type="plugin", source_id="changed")),
    ],
)
def test_mutated_record_identity_data_invalidates_approval(field, value):
    registry, record = _approved_record()
    setattr(record, field, value)
    assert not record.is_approved()
    assert not registry.check_execution(record.identity)[0]


def test_disabled_mutated_definition_cannot_be_reenabled():
    registry, record = _approved_record()
    registry.disable(record.identity)
    record.spec.required_capabilities.append("system:admin")
    with pytest.raises(ValueError, match="no longer has valid approval"):
        registry.reenable(record.identity)
    assert record.status == ResourceStatus.DISABLED


def test_noncanonical_mutation_fails_closed():
    registry, record = _approved_record()
    record.spec.metadata = {"invalid": object()}
    assert not record.is_approved()
    assert not registry.check_execution(record.identity)[0]


def test_definition_change_requires_fresh_validation_and_approval():
    registry, record = _approved_record()
    record.spec.required_capabilities.append("memory:read")
    assert not record.is_approved()
    fingerprint = compute_tool_fingerprint(
        identity=record.identity,
        spec=record.spec,
        provenance=record.provenance,
        implementation_id=record.implementation_id,
        is_local=record.is_local,
    )
    registry.update_definition(
        record.identity,
        spec=record.spec,
        provenance=record.provenance,
        fingerprint=fingerprint,
        implementation_id=record.implementation_id,
        is_local=record.is_local,
    )
    assert not registry.check_execution(record.identity)[0]
    validation = make_validation(record)
    registry.validate(record.identity, validation)
    assert not registry.check_execution(record.identity)[0]
    registry.approve(record.identity, make_approval(record, validation))
    assert registry.check_execution(record.identity)[0]
