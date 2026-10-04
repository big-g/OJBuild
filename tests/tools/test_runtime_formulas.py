"""Formula installation is bounded and retains the runtime approval contract."""

import json

import pytest

from openjarvis.core.types import ToolCall
from openjarvis.tools._stubs import ToolExecutor
from openjarvis.tools.runtime_adapters import (
    evaluate_formula,
    list_adapters,
    parse_formula,
)
from openjarvis.tools.runtime_manager import RuntimeToolManager


def definition(expression="value * 1.8 + 32", variables=None):
    return {
        "name": "custom_formula",
        "description": "Convert supplied temperature to Fahrenheit",
        "adapter_id": "numeric_formula",
        "config": {"expression": expression, "variables": variables or ["value"]},
    }


def execute(executor, params):
    return executor.execute(
        ToolCall(id="formula", name="custom_formula", arguments=json.dumps(params))
    )


def test_formula_restart_edit_and_execution_contract(tmp_path):
    path = tmp_path / "tools.db"
    manager = RuntimeToolManager(path)
    row = manager.store.create(definition(), "user:admin")
    assert manager.available() == []
    assert row["validator_version"] == "formula-v1"
    row = manager.change(row["id"], row["revision"], "approved", "user:admin")
    restarted = RuntimeToolManager(path)
    tool = restarted.available()[0]
    assert tool.spec.parameters["required"] == ["value"]
    assert tool.spec.parameters["properties"]["value"]["maximum"] == 1e12
    assert tool.spec.required_capabilities == ["none"]
    assert tool.spec.evidence_kinds == []
    executor = ToolExecutor([tool])
    assert execute(executor, {"value": 100}).content == "212.0"
    assert execute(executor, {"value": -40}).content == "-40.0"
    assert not execute(executor, {"value": 100, "extra": 1}).success
    assert not execute(executor, {}).success
    for value in (True, "100", None, float("nan"), float("inf"), 1e13):
        assert not execute(executor, {"value": value}).success
    row = manager.change(
        row["id"],
        row["revision"],
        "updated",
        "user:admin",
        definition("(value - 32) / 1.8"),
    )
    assert not execute(executor, {"value": 100}).success
    assert restarted.available() == []
    row = manager.change(row["id"], row["revision"], "approved", "user:admin")
    assert not execute(executor, {"value": 100}).success
    assert float(
        execute(ToolExecutor(restarted.available()), {"value": 212}).content
    ) == pytest.approx(100)
    manager.change(row["id"], row["revision"], "disabled", "user:admin")
    assert restarted.available() == []


@pytest.mark.parametrize(
    "expr",
    [
        '__import__("os").system("echo no")',
        "value.__class__",
        "value[0]",
        "[value for value in range(2)]",
        "(lambda: value)()",
        "value ** 1000000",
        '"x" * value',
        "True + value",
        "value // 2",
        "value % 2",
        "abs(value)",
        "(other + value)",
        "1e400 + value",
        "1e13 + value",
        "value and 2",
        "value if value else 1",
        "{value: 1}",
    ],
)
def test_non_arithmetic_expressions_are_rejected_before_installation(tmp_path, expr):
    manager = RuntimeToolManager(tmp_path / "tools.db")
    with pytest.raises(ValueError):
        manager.store.create(definition(expr), "user:admin")
    assert manager.store.list() == []


@pytest.mark.parametrize(
    "variables",
    [
        [],
        ["value", "value"],
        ["Value"],
        ["_value"],
        ["value", "unused"],
        ["value"] * 9,
        [1],
        [["value"]],
        "value",
    ],
)
def test_variable_contract_is_strict(variables):
    with pytest.raises(ValueError):
        parse_formula({"expression": "value + 1", "variables": variables})


def test_expression_and_runtime_numeric_limits(tmp_path):
    for expr in ("+" * 513, "value" + " + 1" * 50, "-" * 34 + "value"):
        with pytest.raises(ValueError):
            parse_formula({"expression": expr, "variables": ["value"]})
    manager = RuntimeToolManager(tmp_path / "tools.db")
    row = manager.store.create(
        definition("value / divisor", ["value", "divisor"]), "user:admin"
    )
    manager.change(row["id"], 1, "approved", "user:admin")
    executor = ToolExecutor(manager.available())
    assert execute(executor, {"value": 10, "divisor": 2}).content == "5.0"
    assert not execute(executor, {"value": 10, "divisor": 0}).success
    assert not execute(executor, {"value": 10, "divisor": 1e-300}).success
    # A valid denominator may be zero for some inputs; installation must not
    # execute the formula against arbitrary sample values to "validate" it.
    parse_formula(
        {"expression": "value / (divisor - 1)", "variables": ["value", "divisor"]}
    )
    assert (
        evaluate_formula(
            {"expression": "value * value", "variables": ["value"]}, {"value": 1e12}
        )
        == 1e24
    )
    with pytest.raises(ValueError):
        evaluate_formula(
            {"expression": "value * value * value", "variables": ["value"]},
            {"value": 1e12},
        )


def test_original_transform_fingerprint_and_approval_are_preserved(tmp_path):
    manager = RuntimeToolManager(tmp_path / "tools.db")
    # Captured using the published transform-v1 implementation before adapters.
    fingerprint = "01466fb84bea26c757f23330b681b7590b7b72b4d45e6d4d2515d9c34e6b9e10"
    identity = "00000000-0000-4000-8000-000000000001"
    definition_json = json.dumps(
        {"name": "custom_legacy", "description": "Legacy text", "transform": "upper"}
    )
    with manager.store.connection() as db:
        db.execute(
            "INSERT INTO runtime_tools VALUES(?,?,?,?,?,?,?,?,?)",
            (
                identity,
                "custom_legacy",
                definition_json,
                2,
                1,
                fingerprint,
                "user:admin",
                1700000000,
                "transform-v1",
            ),
        )
    row = manager.store.get(identity)
    assert manager.record(row).fingerprint.value == fingerprint
    assert manager.record(row).is_approved()
    assert manager.available()[0].execute(input="Hello").content == "HELLO"
    assert manager.view(row)["config"] == {"transform": "upper"}
    assert manager.store.get(identity)["definition"] == definition_json


def test_adapter_metadata_is_detached_and_contracts_cannot_be_supplied(tmp_path):
    metadata = list_adapters()
    metadata[0]["default_config"]["transform"] = "shell"
    assert list_adapters()[0]["default_config"]["transform"] == "upper"
    manager = RuntimeToolManager(tmp_path / "tools.db")
    for patch in (
        {"adapter_id": "unknown"},
        {"transform": "upper"},
        {"required_capabilities": ["none"]},
    ):
        with pytest.raises(ValueError):
            manager.store.create({**definition(), **patch}, "user:admin")


def test_unavailable_definition_is_quarantined_and_repairable(tmp_path):
    manager = RuntimeToolManager(tmp_path / "tools.db")
    bad = manager.store.create(definition(), "user:admin")
    bad = manager.change(bad["id"], 1, "approved", "user:admin")
    healthy = manager.store.create(
        {"name": "custom_ok", "description": "Keep text", "transform": "identity"},
        "user:admin",
    )
    manager.change(healthy["id"], 1, "approved", "user:admin")
    with manager.store.connection() as db:
        db.execute(
            "UPDATE runtime_tools SET definition='bad JSON' WHERE id=?", (bad["id"],)
        )
    assert [t.spec.name for t in manager.available()] == ["custom_ok"]
    view = manager.view(manager.store.get(bad["id"]))
    assert view["status"] == "invalid"
    assert not view["enabled"] and not view["approved"]
    assert view["config"] == {}
    with pytest.raises(ValueError):
        manager.change(bad["id"], bad["revision"], "approved", "user:admin")
    repaired = manager.change(
        bad["id"], bad["revision"], "updated", "user:admin", definition()
    )
    assert not manager.view(repaired)["approved"]
    with manager.store.connection() as db:
        db.execute(
            "UPDATE runtime_tools SET definition=? WHERE id=?",
            (json.dumps({**definition(), "adapter_id": "retired"}), bad["id"]),
        )
    assert manager.view(manager.store.get(bad["id"]))["status"] == "invalid"
    manager.change(bad["id"], repaired["revision"], "deleted", "user:admin")
    assert manager.store.audit(bad["id"])[0]["event"] == "deleted"
