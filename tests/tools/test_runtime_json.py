"""JSON extraction preserves approval and never evaluates supplied content."""

import json

import pytest

from openjarvis.core.types import ToolCall
from openjarvis.tools._stubs import ToolExecutor
from openjarvis.tools.runtime_json import extract_json, parse_path
from openjarvis.tools.runtime_manager import RuntimeToolManager, RuntimeTransformTool


def definition(path="/forecast/temperature"):
    return {
        "name": "custom_json_field",
        "description": "Select a temperature from supplied JSON",
        "adapter_id": "json_extract",
        "config": {"path": path},
    }


def call(tool, text):
    return ToolExecutor([tool]).execute(
        ToolCall(
            id="json", name="custom_json_field", arguments=json.dumps({"input": text})
        )
    )


def test_restart_edit_disable_remove_and_no_evidence_authority(tmp_path):
    db = tmp_path / "tools.db"
    manager = RuntimeToolManager(db)
    row = manager.store.create(definition(), "user:admin")
    unapproved = RuntimeTransformTool(row, manager)
    text = '{"forecast":{"temperature":18,"humidity":50}}'
    assert not unapproved.execute(input=text).success
    assert not call(unapproved, text).success
    row = manager.change(row["id"], 1, "approved", "user:admin")
    restarted = RuntimeToolManager(db)
    tool = restarted.available()[0]
    assert row["validator_version"] == "json-extract-v1"
    assert tool.spec.required_capabilities == ["none"]
    assert tool.spec.evidence_kinds == []
    assert call(tool, text).content == "18"
    assert tool.spec.parameters["properties"]["input"]["maxLength"] == 32768
    row = manager.change(
        row["id"],
        row["revision"],
        "updated",
        "user:admin",
        definition("/forecast/humidity"),
    )
    assert not call(tool, text).success
    row = manager.change(row["id"], row["revision"], "approved", "user:admin")
    assert not call(
        tool, text
    ).success  # Replacement approval cannot authorize an old path.
    replacement = restarted.available()[0]
    assert call(replacement, text).content == "50"
    row = manager.change(row["id"], row["revision"], "disabled", "user:admin")
    assert not replacement.execute(input=text).success
    row = manager.change(row["id"], row["revision"], "enabled", "user:admin")
    assert call(replacement, text).success
    manager.change(row["id"], row["revision"], "deleted", "user:admin")
    assert not call(replacement, text).success


@pytest.mark.parametrize(
    "path,text,expected",
    [
        ("/items/0/name", '{"items":[{"name":"first"}]}', '"first"'),
        ("/a~1b/~0", '{"a/b":{"~":true}}', "true"),
        ("/", '{"":null}', "null"),
        ("/obj", '{"obj":{"x":[1,false,null]}}', '{"x":[1,false,null]}'),
        ("/name", '{"name":"é"}', '"é"'),
        ("/01", '{"01":"object key"}', '"object key"'),
        (
            "/script",
            '{"script":"__import__(\\"os\\").system(\\"echo no\\")"}',
            '"__import__(\\"os\\").system(\\"echo no\\")"',
        ),
        ("/braces", '{"braces":"' + "[" * 100 + '"}', '"' + "[" * 100 + '"'),
    ],
)
def test_values_paths_and_script_strings_remain_data(path, text, expected):
    assert extract_json({"path": path}, {"input": text}) == expected


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"path": ""},
        {"path": "field"},
        {"path": 1},
        {"path": "/~2"},
        {"path": "/~"},
        {"path": "/" + "a" * 512},
        {"path": "/a" * 33},
        {"path": "/a", "expression": "exec()"},
    ],
)
def test_invalid_paths_rejected_before_installation(tmp_path, config):
    manager = RuntimeToolManager(tmp_path / "tools.db")
    with pytest.raises(ValueError):
        manager.store.create({**definition(), "config": config}, "user:admin")
    assert manager.store.list() == []


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"input": {}},
        {"input": "{}", "extra": 1},
        {"input": '{"a":NaN}'},
        {"input": '{"a":Infinity}'},
        {"input": '{"a":1e400}'},
        {"input": '{"a":1,"a":2}'},
        {"input": '{"a":{"b":1,"b":2}}'},
        {"input": '{"a":1,'},
        {"input": "x" * 32769},
        {"input": "[" * 33 + "0" + "]" * 33},
        {"input": '{"a":[' + ",".join("0" for _ in range(10000)) + "]}"},
        {"input": '{"A":1}'},
        {"input": '{"a":"\\ud800"}'},
    ],
)
def test_invalid_input_fails_without_echoing_content(params):
    with pytest.raises(ValueError):
        extract_json({"path": "/a"}, params)


def test_limit_boundaries_and_missing_values():
    nested = "[" * 31 + "0" + "]" * 31
    assert extract_json({"path": "/a"}, {"input": '{"a":' + nested + "}"}) == nested
    assert parse_path({"path": "/a" * 32}) == ["a"] * 32
    for path in ("/items/-1", "/items/01", "/items/-", "/items/1", "/a/nope"):
        with pytest.raises(ValueError, match="not found"):
            extract_json({"path": path}, {"input": '{"items":[0],"a":null}'})
    with pytest.raises(ValueError) as error:
        extract_json({"path": "/a"}, {"input": '{"a": SECRET-INPUT}'})
    assert "SECRET-INPUT" not in str(error.value)


def test_bad_json_propagates_as_failed_tool_result(tmp_path):
    manager = RuntimeToolManager(tmp_path / "tools.db")
    row = manager.store.create(definition("/a"), "user:admin")
    manager.change(row["id"], 1, "approved", "user:admin")
    tool = manager.available()[0]
    for text in ('{"a":NaN}', '{"a":1,"a":2}', '{"a":SECRET-INPUT}', '{"b":1}'):
        result = call(tool, text)
        assert not result.success
        assert "SECRET-INPUT" not in result.content
