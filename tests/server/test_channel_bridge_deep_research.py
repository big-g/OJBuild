"""Test ChannelBridge routes to DeepResearchAgent."""

from __future__ import annotations

from unittest.mock import MagicMock


def test_handle_chat_uses_deep_research_agent() -> None:
    """When a DeepResearch agent is configured, route through it."""
    from openjarvis.server.channel_bridge import ChannelBridge
    from openjarvis.server.session_store import SessionStore

    mock_agent = MagicMock()
    mock_agent.run.return_value = MagicMock(content="Found 3 results about Spain.")

    bridge = ChannelBridge(
        channels={},
        session_store=SessionStore(db_path=":memory:"),
        bus=MagicMock(),
        deep_research_agent=mock_agent,
    )

    result = bridge.handle_incoming(
        sender_id="+15551234567",
        content="When was my last trip to Spain?",
        channel_type="twilio",
    )

    assert "Spain" in result
    mock_agent.run.assert_called_once()


def test_handle_chat_grounds_deep_research_before_publish() -> None:
    from openjarvis.agents._stubs import AgentResult
    from openjarvis.core.types import ToolResult
    from openjarvis.server.channel_bridge import ChannelBridge
    from openjarvis.server.session_store import SessionStore
    from openjarvis.tools._stubs import ToolSpec

    class _Tool:
        @property
        def spec(self):
            return ToolSpec(
                name="bridge_evidence",
                description="Bridge evidence test tool.",
                evidence_kinds=["current"],
            )

    class _Engine:
        def generate(self, messages, *, model, temperature, max_tokens, **kwargs):
            return {
                "content": (
                    '{"supported": false, '
                    '"unsupported_claims": ["Sunny conditions are not in evidence."], '
                    '"reason": "The condition is unsupported."}'
                )
            }

    mock_agent = MagicMock()
    mock_agent._tools = [_Tool()]
    mock_agent._engine = _Engine()
    mock_agent._model = "test-model"
    mock_agent.run.return_value = AgentResult(
        content="Tomorrow's high will be 82°F and it will be sunny.",
        tool_results=[
            ToolResult(
                tool_name="bridge_evidence",
                content="Forecast: tomorrow high 82°F.",
                success=True,
                metadata={
                    "evidence": {
                        "provider": "test",
                        "records": [
                            {
                                "source": "weather",
                                "content": "Forecast: tomorrow high 82°F.",
                            }
                        ],
                    }
                },
            )
        ],
    )

    bridge = ChannelBridge(
        channels={},
        session_store=SessionStore(db_path=":memory:"),
        bus=MagicMock(),
        deep_research_agent=mock_agent,
    )

    result = bridge.handle_incoming(
        sender_id="+15551234567",
        content="What's the weather forecast for tomorrow?",
        channel_type="twilio",
    )

    assert result == "I couldn't verify the response against the retrieved evidence."
    assert "sunny" not in result


def test_handle_chat_falls_back_to_system() -> None:
    """When no DeepResearch agent, fall back to system.ask()."""
    from openjarvis.server.channel_bridge import ChannelBridge
    from openjarvis.server.session_store import SessionStore

    mock_system = MagicMock()
    mock_system.ask.return_value = {"content": "Generic response"}

    bridge = ChannelBridge(
        channels={},
        session_store=SessionStore(db_path=":memory:"),
        bus=MagicMock(),
        system=mock_system,
    )

    result = bridge.handle_incoming(
        sender_id="+15551234567",
        content="Hello",
        channel_type="twilio",
    )

    assert result == "Generic response"
    mock_system.ask.assert_called_once()
