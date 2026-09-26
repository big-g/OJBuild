"""Browser research uses the active executor and gates answers before emission."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openjarvis.agents.research_loop import ResearchAgent
from openjarvis.core.types import ToolResult
from openjarvis.server.research_router import _research_web_access
from openjarvis.tools._stubs import ToolExecutor
from openjarvis.tools.web_search import WebSearchTool


class Engine:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def generate(self, messages, **kwargs):
        self.calls.append((list(messages), kwargs))
        return next(self.responses)


def web_call():
    return {"tool_calls": [{"id": "web-1", "name": "web_search",
                            "arguments": '{"query": "launch status"}'}]}


def verdict(supported=True):
    return {"content": json.dumps({"supported": supported,
                                   "unsupported_claims": [] if supported else ["status"],
                                   "reason": "checked"})}


def runtime(*, allowed=True, failed=False, records=None):
    tool = WebSearchTool()
    tool.execute = Mock(return_value=ToolResult(
        tool_name="web_search", success=not failed,
        content="Web search failed." if failed else "Search results.",
        metadata={"evidence": {"records": records if records is not None else [
            {"source": "web", "url": "https://source-a.test/status",
             "content": "The launch is approved."},
        ]}},
    ))
    # Exercise the real executor with a recording policy seam. Native-policy
    # integration is tested separately when the Rust extension is installed.
    policy = SimpleNamespace(
        resolve_effective_tool_capabilities=lambda tool, params: tool.spec.required_capabilities,
        check=Mock(return_value=allowed),
    )
    active = SimpleNamespace(
        _tools=[tool],
        _executor=ToolExecutor([tool], capability_policy=policy, agent_id="orchestrator"),
    )
    return active, tool


def research(active, responses, **kwargs):
    spec, execute = _research_web_access(active)
    search = SimpleNamespace(search=Mock(return_value=[]), _store=None)
    events = []
    engine = Engine(responses)
    agent = ResearchAgent(
        engine, search, model="test", web_tool_spec=spec, execute_web=execute,
        validate_evidence=True, on_event=events.append, **kwargs,
    )
    result = agent.run("Search the web for the latest launch status.")
    return result, engine, events


def test_web_research_uses_governed_executor_and_cites_sources():
    active, tool = runtime()
    result, engine, events = research(active, [
        web_call(), {"content": "The launch is approved. [1]"}, verdict(),
    ])
    assert result.answer == "The launch is approved. [1]"
    tool.execute.assert_called_once_with(query="launch status")
    active._executor._capability_policy.check.assert_called_once_with(
        "orchestrator", "network:fetch", "web_search",
    )
    assert "web_search" in [t["function"]["name"] for t in engine.calls[0][1]["tools"]]
    assert events[-1]["type"] == "final_answer"
    assert events[-1]["sources"][0]["url"] == "https://source-a.test/status"


@pytest.mark.parametrize("mode", ["denied", "failed", "empty"])
def test_web_research_cannot_use_failed_or_denied_output_as_evidence(mode):
    active, tool = runtime(allowed=mode != "denied", failed=mode == "failed",
                           records=[] if mode == "empty" else None)
    result, _, events = research(active, [web_call(), {"content": "The launch is approved."}])
    assert result.answer == "I couldn't retrieve the required data."
    assert events[-1]["text"] == result.answer
    assert events[-1]["sources"] == []
    if mode == "denied":
        tool.execute.assert_not_called()


def test_web_research_blocks_unsupported_answer_before_final_event():
    active, _ = runtime()
    result, _, events = research(active, [
        web_call(), {"content": "The launch is canceled."}, verdict(False),
    ])
    assert result.answer == "I couldn't verify the response against the retrieved evidence."
    assert all(e.get("text") != "The launch is canceled." for e in events)


def test_web_research_blocks_cross_source_conflict():
    active, _ = runtime(records=[
        {"content": "The launch is approved.", "url": "https://a.test/status"},
        {"content": "The launch is canceled.", "url": "https://b.test/status"},
    ])
    result, _, _ = research(active, [web_call(), {"content": "The launch is approved."},
        {"content": json.dumps({"conflicting": True, "conflicts": [
            {"claim": "Launch status", "source_ids": ["E1", "E2"],
             "values": ["approved", "canceled"]}], "reason": "Different status"})},
    ])
    assert result.answer == "The available sources conflict."


def test_personal_and_web_results_share_citations_and_validation():
    from openjarvis.connectors.hybrid_search import SearchHit

    active, tool = runtime()
    spec, execute = _research_web_access(active)
    personal = SearchHit(
        chunk_id="chunk-1", document_id="note-1", chunk_idx=0, title="Launch",
        content_snippet="The launch is approved.", source="notes", timestamp="",
        participants=[], score=1, bm25_score=1, vector_score=0,
    )
    search = SimpleNamespace(search=Mock(return_value=[personal]), _store=None)
    engine = Engine([
        {"tool_calls": [{"id": "personal-1", "name": "search",
                          "arguments": '{"query": "launch"}'}]},
        web_call(), {"content": "The launch is approved. [1] [2]"},
        {"content": json.dumps({"conflicting": False, "conflicts": [],
                                "reason": "Both sources agree"})}, verdict(),
    ])
    events = []
    agent = ResearchAgent(engine, search, model="test", web_tool_spec=spec,
                          execute_web=execute, on_event=events.append)
    result = agent.run("Compare my launch note with the public launch status.")
    assert result.answer == "The launch is approved. [1] [2]"
    assert result.evidence_metadata["grounding_status"] == "supported"
    assert [source["ref"] for source in events[-1]["sources"]] == [1, 2]
    search.search.assert_called_once()
    tool.execute.assert_called_once()


def test_web_research_honors_governance_hook():
    active, tool = runtime()
    active._check_tool_allowed = lambda call: ToolResult(
        tool_name=call.name, success=False, content="Denied by governance.",
    )
    result, _, _ = research(active, [web_call(), {"content": "The launch is approved."}])
    tool.execute.assert_not_called()
    assert result.answer == "I couldn't retrieve the required data."


def test_web_tool_is_not_invented_when_runtime_lacks_it():
    assert _research_web_access(None) == (None, None)
    result, engine, _ = research(None, [{"content": "The launch is approved."}] * 2)
    assert "web_search" not in [t["function"]["name"] for t in engine.calls[0][1]["tools"]]
    assert result.answer == "I couldn't retrieve the required data."


def test_web_research_enforces_budget_even_if_model_keeps_requesting_tools():
    active, tool = runtime()
    result, _, _ = research(active, [
        web_call(), web_call(), {"content": "The launch is approved."}, verdict(),
    ], max_iterations=1)
    tool.execute.assert_called_once()
    assert result.answer == "The launch is approved."


def test_web_research_preserves_native_capability_denial():
    pytest.importorskip("openjarvis_rust")
    from openjarvis.security.capabilities import CapabilityPolicy

    active, tool = runtime()
    active._executor._capability_policy = CapabilityPolicy(default_deny=True)
    result, _, _ = research(active, [web_call(), {"content": "The launch is approved."}])
    tool.execute.assert_not_called()
    assert result.answer == "I couldn't retrieve the required data."


@pytest.mark.parametrize("use_web", [False, True])
def test_browser_stream_wires_web_and_emits_only_validated_answer(monkeypatch, use_web):
    import asyncio
    from openjarvis.server import research_router

    active, _ = runtime()
    responses = ([web_call(), {"content": "The launch is approved. [1]"}, verdict()]
                 if use_web else [{"content": "The launch is approved."}] * 2)
    engine = Engine(responses)
    monkeypatch.setattr(research_router, "load_config", lambda: None)
    monkeypatch.setattr(research_router, "_build_planner_engine",
                        lambda *args, **kwargs: ("test", engine, "test"))
    monkeypatch.setattr(research_router, "KnowledgeStore", lambda: None)
    monkeypatch.setattr(research_router, "OllamaEmbedder",
                        lambda: SimpleNamespace(is_available=lambda: False))
    monkeypatch.setattr(research_router, "HybridSearch", lambda *args:
                        SimpleNamespace(search=Mock(return_value=[]), _store=None))
    monkeypatch.setattr(research_router, "_record_research_telemetry", lambda **kwargs: None)

    async def collect():
        return [json.loads(frame.removeprefix("data: ")) async for frame in
                research_router._stream_research("Latest launch status?", active_agent=active)]

    events = asyncio.run(collect())
    answer = "".join(e["text"] for e in events if e["type"] == "synthesis").strip()
    assert answer == ("The launch is approved. [1]" if use_web
                      else "I couldn't retrieve the required data.")
    assert events[-1]["type"] == "done"
    assert events[-1]["evidence"]["evidence_status"] == (
        "obtained" if use_web else "required_not_obtained"
    )


def test_research_route_persists_session_exchange(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openjarvis.server import research_router
    from openjarvis.server.auth_store import AuthStore
    from openjarvis.sessions.session import SessionStore

    app = FastAPI()
    app.include_router(research_router.router)
    auth_store = AuthStore(tmp_path / "auth.db")
    auth_store.create_user(
        user_id="research-user",
        username="research-user",
        password="test-password",
    )
    auth_store.create_user(
        user_id="other-user",
        username="other-user",
        password="test-password",
    )
    token = auth_store.create_session("research-user")
    other_token = auth_store.create_session("other-user")
    session_store = SessionStore(db_path=tmp_path / "sessions.db")
    project = session_store.create_project("research-user", "Default")
    session = session_store.create_session(
        "research-user",
        project.project_id,
        title="Research",
    )
    app.state.auth_store = auth_store
    app.state.session_store = session_store

    async def fake_stream(query, **kwargs):
        yield 'data: {"type":"synthesis","text":"Verified "}\n\n'
        yield 'data: {"type":"synthesis","text":"answer."}\n\n'
        yield 'data: {"type":"done","sources":[]}\n\n'

    monkeypatch.setattr(research_router, "_stream_research", fake_stream)
    client = TestClient(app)
    denied = client.post(
        "/api/research",
        headers={"X-OpenJarvis-Session": other_token},
        json={"query": "Check this claim", "session_id": session.session_id},
    )
    assert denied.status_code == 404

    response = client.post(
        "/api/research",
        headers={"X-OpenJarvis-Session": token},
        json={"query": "Check this claim", "session_id": session.session_id},
    )

    assert response.status_code == 200
    saved = session_store.get_session(session.session_id)
    assert saved is not None
    assert [(message.role, message.content) for message in saved.messages] == [
        ("user", "Check this claim"),
        ("assistant", "Verified answer."),
    ]
    assert saved.messages[1].metadata == {"isResearch": True}
    session_store.close()


def test_research_retries_premature_answer_before_emitting_it():
    active, tool = runtime()
    result, engine, events = research(active, [
        {"content": "I cannot search the internet."}, web_call(),
        {"content": "The launch is approved. [1]"}, verdict(),
    ])
    assert result.answer == "The launch is approved. [1]"
    tool.execute.assert_called_once()
    assert all(e.get("text") != "I cannot search the internet." for e in events)
    assert engine.calls[0][1]["require_tools"] is True
    assert "No evidence has been retrieved" in engine.calls[1][0][-1].content


def test_research_retries_only_once_when_planner_refuses_to_search():
    active, tool = runtime()
    result, engine, events = research(active, [{"content": "The launch is approved."}] * 2)
    assert len(engine.calls) == 2
    assert result.answer == "I couldn't retrieve the required data."
    tool.execute.assert_not_called()
    assert [e["text"] for e in events if e["type"] == "final_answer"] == [result.answer]


def test_empty_personal_search_can_recover_with_governed_web_search():
    active, tool = runtime()
    personal_call = {"tool_calls": [{"id": "personal", "name": "search",
                                    "arguments": '{"query": "launch status"}'}]}
    result, engine, events = research(active, [
        personal_call, {"content": "No information is available."},
        web_call(), {"content": "The launch is approved. [1]"}, verdict(),
    ])
    assert result.answer == "The launch is approved. [1]"
    assert [i.tool_name for i in result.tool_calls] == ["search", "web_search"]
    tool.execute.assert_called_once()
    assert "An empty personal search" in engine.calls[2][0][-1].content
    assert all(e.get("text") != "No information is available." for e in events)


def test_empty_personal_search_recovery_remains_bounded():
    active, tool = runtime()
    personal_call = {"tool_calls": [{"id": "personal", "name": "search",
                                    "arguments": '{"query": "launch status"}'}]}
    result, engine, _ = research(active, [
        personal_call, {"content": "No information."}, {"content": "No information."},
    ])
    assert len(engine.calls) == 3
    assert result.answer == "I couldn't retrieve the required data."
    tool.execute.assert_not_called()


def test_unknown_tool_and_blocked_answer_have_diagnostics_without_query(caplog):
    active, tool = runtime()
    unknown = {"tool_calls": [{"id": "unknown", "name": "browse",
                               "arguments": '{"query": "PRIVATE_SENTINEL"}'}]}
    result, _, _ = research(active, [unknown, unknown,
                                    {"content": "The launch is approved."}], max_iterations=1)
    assert result.answer == "I couldn't retrieve the required data."
    tool.execute.assert_not_called()
    assert "unrecognized tool name='browse'" in caplog.text
    assert "web_available=True completed_searches=0 records=0" in caplog.text
    assert "PRIVATE_SENTINEL" not in caplog.text
