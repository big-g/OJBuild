import importlib.util
import json
import sqlite3
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "diagnostic", Path(__file__).parents[2] / "scripts/diagnose_tool_retrieval.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_read_only_trace_summary_excludes_arguments_and_results(tmp_path):
    trace = tmp_path / "traces.db"
    with sqlite3.connect(trace) as db:
        db.executescript("""
            CREATE TABLE traces(trace_id, query, model, agent, metadata, started_at);
            CREATE TABLE trace_steps(
                trace_id, step_type, step_index, input, output, metadata
            );
        """)
        db.execute(
            "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?)",
            (
                "one",
                "What is the forecast?",
                "model",
                "orchestrator",
                '{"evidence":{"status":"required_not_obtained"}}',
                1,
            ),
        )
        db.execute(
            "INSERT INTO trace_steps VALUES (?, ?, ?, ?, ?, ?)",
            (
                "one",
                "tool_call",
                0,
                '{"tool":"custom_mcp_weather","arguments":"PRIVATE_ARGUMENT"}',
                '{"success":false,"result":"PRIVATE_RESULT"}',
                "{}",
            ),
        )
    original = trace.read_bytes()
    result = module.diagnose(trace, tmp_path / "absent.db")
    assert result["attempts"][0]["tool_calls"][0]["success"] is False
    assert result["attempts"][0]["evidence"]["status"] == "required_not_obtained"
    assert "PRIVATE" not in json.dumps(result)
    assert trace.read_bytes() == original
    assert not (tmp_path / "absent.db").exists()


def test_missing_databases_are_not_created(tmp_path):
    assert (
        module.diagnose(tmp_path / "trace.db", tmp_path / "mcp.db")["trace_database"]
        == "missing"
    )
    assert list(tmp_path.iterdir()) == []


def test_connection_settings_report_excludes_credentials_and_endpoint(tmp_path):
    from openjarvis.mcp.runtime_store import RuntimeMCPStore

    path = tmp_path / "mcp.db"
    store = RuntimeMCPStore(path)
    store.create(
        {
            "name": "weather",
            "url": "https://example.com/mcp",
            "allow_without_confirmation": True,
            "evidence_tools": {"lookup": ["current"]},
        },
        "admin",
        "PRIVATE_SECRET",
    )
    result = module.diagnose(tmp_path / "absent.db", path)
    row = result["mcp_connections"][0]
    assert row["automatic_calls"] is True
    assert row["approved"] is False
    assert row["evidence_tools"] == {"lookup": ["current"]}
    assert "PRIVATE_SECRET" not in json.dumps(result)
    assert "https://example.com" not in json.dumps(result)
