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


class _SystemEvidenceTool:
    tool_id = "system_evidence"

    @property
    def spec(self):
        from openjarvis.tools._stubs import ToolSpec

        return ToolSpec(
            name="system_evidence",
            description="System evidence test tool.",
            evidence_kinds=["current", "external"],
        )


class _SystemEvidenceAgent:
    accepts_tools = True
    answer = "Tomorrow's high will be 82°F."
    evidence_content = "Forecast: tomorrow high 82°F."

    def __init__(self, engine, model, *, tools=None, **kwargs):
        self._engine = engine
        self._model = model
        self._tools = list(tools or [])

    def run(self, query, context=None):
        from openjarvis.agents._stubs import AgentResult
        from openjarvis.core.types import ToolResult

        return AgentResult(
            content=self.answer,
            turns=1,
            tool_results=[
                ToolResult(
                    tool_name="system_evidence",
                    content=self.evidence_content,
                    success=True,
                    metadata={
                        "evidence": {
                            "provider": "test",
                            "records": [
                                {
                                    "source": "test-source",
                                    "content": self.evidence_content,
                                }
                            ],
                        }
                    },
                )
            ],
        )


class TestSystemEvidenceBoundary:
    def test_non_orchestrator_agent_cannot_bypass_current_evidence_gate(self):
        from openjarvis.agents._stubs import AgentResult
        from openjarvis.core.registry import AgentRegistry

        class _UngroundedAgent:
            accepts_tools = False

            def __init__(self, engine, model, **kwargs):
                self._engine = engine
                self._model = model

            def run(self, query, context=None):
                return AgentResult(
                    content="Tomorrow will be sunny and 82°F.",
                    turns=1,
                )

        AgentRegistry.register_value(
            "ungrounded_evidence_test_agent",
            _UngroundedAgent,
        )

        system = _FakeSystem(
            engine=_FakeEngine({"content": ""}),
            agent_name="ungrounded_evidence_test_agent",
        )
        orchestrator = QueryOrchestrator(system)

        result = orchestrator.ask(
            "What's the weather forecast for tomorrow?",
            context=False,
        )

        assert result["content"] == "I couldn't retrieve the required data."
        assert result["metadata"]["evidence_status"] == "required_not_obtained"


    def test_supported_claim_survives_semantic_grounding(self):
        from openjarvis.core.registry import AgentRegistry

        class _SupportedAgent(_SystemEvidenceAgent):
            answer = "Tomorrow's high will be 82°F."

        AgentRegistry.register_value("supported_grounding_agent", _SupportedAgent)
        engine = _FakeEngine(
            {
                "content": (
                    '{"supported": true, "unsupported_claims": [], '
                    '"reason": "The claim is supported."}'
                )
            }
        )
        system = _FakeSystem(
            engine=engine,
            agent_name="supported_grounding_agent",
            tools=[_SystemEvidenceTool()],
        )

        result = QueryOrchestrator(system).ask(
            "What's the weather forecast for tomorrow?",
            context=False,
        )

        assert result["content"] == "Tomorrow's high will be 82°F."
        assert result["metadata"]["evidence_status"] == "obtained"
        assert result["metadata"]["grounding_status"] == "supported"
        assert result["metadata"]["grounding_method"] == "llm_judge"
        assert len(engine.calls) == 1

    def test_changed_numeric_claim_is_blocked_before_validator_call(self):
        from openjarvis.core.registry import AgentRegistry

        class _WrongNumberAgent(_SystemEvidenceAgent):
            answer = "Tomorrow's high will be 91°F."

        AgentRegistry.register_value("wrong_number_grounding_agent", _WrongNumberAgent)
        engine = _FakeEngine(
            {
                "content": (
                    '{"supported": true, "unsupported_claims": [], '
                    '"reason": "should not be used"}'
                )
            }
        )
        system = _FakeSystem(
            engine=engine,
            agent_name="wrong_number_grounding_agent",
            tools=[_SystemEvidenceTool()],
        )

        result = QueryOrchestrator(system).ask(
            "What's the weather forecast for tomorrow?",
            context=False,
        )

        assert result["content"] == (
            "I couldn't verify the response against the retrieved evidence."
        )
        assert result["metadata"]["evidence_status"] == "obtained"
        assert result["metadata"]["grounding_status"] == "unsupported"
        assert result["metadata"]["grounding_method"] == "numeric_anchor"
        assert engine.calls == []

    def test_semantically_unsupported_claim_is_blocked(self):
        from openjarvis.core.registry import AgentRegistry

        class _SunnyAgent(_SystemEvidenceAgent):
            answer = "Tomorrow's high will be 82°F and it will be sunny."

        AgentRegistry.register_value("sunny_grounding_agent", _SunnyAgent)
        engine = _FakeEngine(
            {
                "content": (
                    '{"supported": false, '
                    '"unsupported_claims": ["Sunny conditions are not in evidence."], '
                    '"reason": "The weather condition is unsupported."}'
                )
            }
        )
        system = _FakeSystem(
            engine=engine,
            agent_name="sunny_grounding_agent",
            tools=[_SystemEvidenceTool()],
        )

        result = QueryOrchestrator(system).ask(
            "What's the weather forecast for tomorrow?",
            context=False,
        )

        assert result["content"] == (
            "I couldn't verify the response against the retrieved evidence."
        )
        assert result["metadata"]["grounding_status"] == "unsupported"
        assert result["metadata"]["grounding_method"] == "llm_judge"
        assert result["metadata"]["grounding_unsupported_claims"] == [
            "Sunny conditions are not in evidence."
        ]
        assert len(engine.calls) == 1

    def test_cross_source_conflict_blocks_before_grounding(self):
        from openjarvis.agents._stubs import AgentResult
        from openjarvis.core.registry import AgentRegistry
        from openjarvis.core.types import ToolResult

        class _ConflictAgent(_SystemEvidenceAgent):
            def run(self, query, context=None):
                return AgentResult(
                    content="Tomorrow's weather condition is sunny.",
                    turns=1,
                    tool_results=[
                        ToolResult(
                            tool_name="system_evidence",
                            content="conflicting weather reports",
                            success=True,
                            metadata={
                                "evidence": {
                                    "provider": "test",
                                    "records": [
                                        {
                                            "url": "https://source-a.test/weather",
                                            "content": (
                                                "Tomorrow's weather condition "
                                                "is sunny."
                                            ),
                                        },
                                        {
                                            "url": "https://source-b.test/weather",
                                            "content": (
                                                "Tomorrow's weather condition "
                                                "is rainy."
                                            ),
                                        },
                                    ],
                                }
                            },
                        )
                    ],
                )

        AgentRegistry.register_value(
            "cross_source_conflict_agent",
            _ConflictAgent,
        )
        engine = _FakeEngine(
            {
                "content": (
                    '{"conflicting": true, "conflicts": ['
                    '{"claim": "Weather condition", '
                    '"source_ids": ["E1", "E2"], '
                    '"values": ["sunny", "rainy"]}], '
                    '"reason": "The sources disagree on conditions."}'
                )
            }
        )
        system = _FakeSystem(
            engine=engine,
            agent_name="cross_source_conflict_agent",
            tools=[_SystemEvidenceTool()],
        )

        result = QueryOrchestrator(system).ask(
            "What's the weather forecast for tomorrow?",
            context=False,
        )

        assert result["content"] == "The available sources conflict."
        assert result["metadata"]["evidence_status"] == "conflicting"
        assert result["metadata"]["evidence_conflict_status"] == "conflicting"
        assert result["metadata"]["evidence_conflict_method"] == "llm_judge"
        assert result["metadata"]["evidence_conflict_claims"] == [
            "Weather condition: sunny vs rainy"
        ]
        assert "grounding_status" not in result["metadata"]
        assert len(engine.calls) == 1

    def test_grounding_validator_failure_blocks_response(self):
        from openjarvis.core.registry import AgentRegistry

        AgentRegistry.register_value(
            "malformed_grounding_agent",
            _SystemEvidenceAgent,
        )
        engine = _FakeEngine({"content": "not json"})
        system = _FakeSystem(
            engine=engine,
            agent_name="malformed_grounding_agent",
            tools=[_SystemEvidenceTool()],
        )

        result = QueryOrchestrator(system).ask(
            "What's the weather forecast for tomorrow?",
            context=False,
        )

        assert result["content"] == (
            "I couldn't verify the response against the retrieved evidence."
        )
        assert result["metadata"]["grounding_status"] == "validation_failed"


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
