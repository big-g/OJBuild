"""Isolated QueryOrchestrator tests using a minimal fake system."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest

from openjarvis.core.config import JarvisConfig
from openjarvis.core.events import EventBus
from openjarvis.system import QueryOrchestrator


class _FakeEngine:
    def __init__(self, reply: Dict[str, Any]) -> None:
        self._reply = reply
        self.calls: List[Dict[str, Any]] = []

    def generate(self, messages, *, model, temperature, max_tokens, **_):
        self.calls.append(
            {
                "messages": list(messages),
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        return self._reply

    def list_models(self):
        return []


@dataclass
class _FakeSystem:
    """Minimum surface QueryOrchestrator reads — no subsystems wired."""

    config: JarvisConfig = field(default_factory=JarvisConfig)
    bus: EventBus = field(default_factory=EventBus)
    engine: Any = None
    engine_key: str = "fake"
    model: str = "fake-model"
    agent_name: str = ""
    tools: List[Any] = field(default_factory=list)
    memory_backend: Optional[Any] = None
    capability_policy: Optional[Any] = None
    capability_registry: Optional[Any] = None
    tool_management_registry: Optional[Any] = None
    session_store: Optional[Any] = None
    trace_store: Optional[Any] = None
    trace_collector: Optional[Any] = None
    _skill_few_shot_examples: Optional[List[str]] = None


class TestAskDirectEngineMode:
    def test_direct_engine_returns_content(self):
        engine = _FakeEngine({"content": "hi there", "usage": {"tokens": 4}})
        system = _FakeSystem(engine=engine)
        orchestrator = QueryOrchestrator(system)

        result = orchestrator.ask("hello", context=False)

        assert result["content"] == "hi there"
        assert result["usage"] == {"tokens": 4}
        assert result["model"] == "fake-model"
        assert result["engine"] == "fake"

    def test_current_direct_query_is_blocked_before_engine_call(self):
        engine = _FakeEngine(
            {
                "content": "Tomorrow will be sunny and 82°F.",
            }
        )
        system = _FakeSystem(engine=engine)
        orchestrator = QueryOrchestrator(system)

        result = orchestrator.ask(
            "What's the weather forecast for tomorrow?",
            context=False,
        )

        assert result["content"] == "I couldn't retrieve the required data."
        assert result["metadata"]["evidence_status"] == "required_not_obtained"
        assert engine.calls == []

    def test_current_direct_query_records_evidence_trace(
        self,
        tmp_path,
    ):
        from openjarvis.traces.store import TraceStore

        engine = _FakeEngine(
            {
                "content": "Unsupported current answer.",
            }
        )
        store = TraceStore(tmp_path / "system-direct-evidence.db")
        system = _FakeSystem(
            engine=engine,
            trace_store=store,
        )
        orchestrator = QueryOrchestrator(system)

        result = orchestrator.ask(
            "What's the weather forecast for tomorrow?",
            context=False,
        )

        assert result["content"] == "I couldn't retrieve the required data."
        assert engine.calls == []
        traces = store.list_traces()
        assert len(traces) == 1
        assert traces[0].metadata["evidence"] == {
            "required": True,
            "kind": "current",
            "status": "required_not_obtained",
            "reason": "Required evidence was not obtained.",
            "records": 0,
        }
        store.close()

    def test_forwards_temperature_and_max_tokens(self):
        engine = _FakeEngine({"content": ""})
        system = _FakeSystem(engine=engine)
        orchestrator = QueryOrchestrator(system)

        orchestrator.ask("q", context=False, temperature=0.9, max_tokens=128)

        assert engine.calls[0]["temperature"] == 0.9
        assert engine.calls[0]["max_tokens"] == 128

    def test_uses_config_defaults_when_omitted(self):
        engine = _FakeEngine({"content": ""})
        config = JarvisConfig()
        config.intelligence.temperature = 0.42
        config.intelligence.max_tokens = 77
        system = _FakeSystem(config=config, engine=engine)
        orchestrator = QueryOrchestrator(system)

        orchestrator.ask("q", context=False)

        assert engine.calls[0]["temperature"] == 0.42
        assert engine.calls[0]["max_tokens"] == 77


class TestAskAgentRouting:
    def test_agent_none_stays_on_engine(self):
        engine = _FakeEngine({"content": "engine path"})
        system = _FakeSystem(engine=engine, agent_name="none")
        orchestrator = QueryOrchestrator(system)

        result = orchestrator.ask("plain question", context=False)

        assert result["content"] == "engine path"
        assert len(engine.calls) == 1

    def test_unknown_agent_returns_error_dict(self):
        engine = _FakeEngine({"content": ""})
        system = _FakeSystem(engine=engine, agent_name="does_not_exist")
        orchestrator = QueryOrchestrator(system)

        result = orchestrator.ask("q", context=False)

        assert result.get("error") is True
        assert "does_not_exist" in result["content"]


class TestDetectAgentIntent:
    @pytest.mark.parametrize(
        "query",
        [
            "good morning",
            "morning digest please",
            "can you run the daily briefing",
            "morning briefing time",
        ],
    )
    def test_morning_digest_triggers(self, query):
        from openjarvis.core.registry import AgentRegistry

        # Register a stub so the intent check returns the name.
        try:
            AgentRegistry.get("morning_digest")
            registered = True
        except KeyError:
            registered = False

        system = _FakeSystem()
        orchestrator = QueryOrchestrator(system)
        detected = orchestrator._detect_agent_intent(query)

        if registered:
            assert detected == "morning_digest"
        else:
            assert detected is None

    def test_plain_query_returns_none(self):
        system = _FakeSystem()
        orchestrator = QueryOrchestrator(system)
        assert orchestrator._detect_agent_intent("what's the weather") is None



class TestAgentConstructionIntegrity:
    def test_security_configuration_is_not_silently_dropped(self):
        from openjarvis.core.registry import AgentRegistry
        from openjarvis.core.types import ToolResult

        class _UnsafeToolAgent:
            accepts_tools = True

            def __init__(
                self,
                engine,
                model,
                *,
                tools=None,
                bus=None,
                max_turns=None,
                temperature=None,
                max_tokens=None,
            ):
                raise AssertionError(
                    "constructor must not run without capability_policy support"
                )

            def run(self, query, context=None):
                raise AssertionError("agent must not run")

        AgentRegistry.register_value("unsafe_security_agent", _UnsafeToolAgent)

        system = _FakeSystem(
            engine=_FakeEngine({"content": ""}),
            agent_name="unsafe_security_agent",
            capability_policy=object(),
        )
        orchestrator = QueryOrchestrator(system)

        result = orchestrator.ask("use a tool", context=False)

        assert result["error"] is True
        assert "does not accept required security configuration" in result["content"]


    def test_constructor_type_error_does_not_trigger_fallback_retry(self):
        from openjarvis.core.registry import AgentRegistry

        calls = []

        class _BrokenAgent:
            accepts_tools = False

            def __init__(
                self,
                engine,
                model,
                *,
                bus=None,
                temperature=None,
                max_tokens=None,
            ):
                calls.append((engine, model))
                raise TypeError("constructor bug")

        AgentRegistry.register_value("broken_constructor_agent", _BrokenAgent)

        system = _FakeSystem(
            engine=_FakeEngine({"content": ""}),
            agent_name="broken_constructor_agent",
        )
        orchestrator = QueryOrchestrator(system)

        result = orchestrator.ask("hello", context=False)

        assert result["error"] is True
        assert "constructor bug" in result["content"]
        assert len(calls) == 1
