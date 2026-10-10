"""Offline regressions for keyless retrieval and forecast page evidence."""

import sys
from unittest.mock import MagicMock

import httpx
import pytest

import openjarvis.tools.web_search as web_search
from openjarvis.core.evidence import assess_tool_results, detect_evidence_requirement
from openjarvis.tools.web_search import WebSearchTool


@pytest.fixture
def keyless(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    ddgs = MagicMock()
    module = MagicMock()
    module.DDGS.return_value = ddgs
    monkeypatch.setitem(sys.modules, "ddgs", module)
    return WebSearchTool(), ddgs, module


def source():
    return {
        "title": "Kernersville forecast",
        "href": "https://forecast.example.test/kernersville",
        "body": "See the daily forecast.",
    }


def test_no_key_skips_tavily_entirely(keyless, monkeypatch):
    tool, ddgs, module = keyless
    tavily = MagicMock()
    monkeypatch.setitem(sys.modules, "tavily", tavily)
    ddgs.text.return_value = [source()]
    result = tool.execute(query="Kernersville NC weather", fetch_pages=False)
    assert result.success
    tavily.TavilyClient.assert_not_called()
    module.DDGS.assert_called_once_with(timeout=8)
    assert ddgs.text.call_args.kwargs["backend"] == "duckduckgo"


@pytest.mark.parametrize("first", [[], TimeoutError("provider offline")])
def test_empty_or_failed_provider_tries_next(keyless, first):
    tool, ddgs, _ = keyless
    ddgs.text.side_effect = [first, [source()]]
    result = tool.execute(query="Kernersville NC weather", fetch_pages=False)
    assert result.success and result.metadata["engine"] == "bing"
    assert [c.kwargs["backend"] for c in ddgs.text.call_args_list] == [
        "duckduckgo",
        "bing",
    ]


def test_empty_tavily_falls_back(keyless, monkeypatch):
    tool, ddgs, _ = keyless
    tool._api_key = "offline-test-key"
    tavily = MagicMock()
    tavily.TavilyClient.return_value.search.return_value = {"results": []}
    monkeypatch.setitem(sys.modules, "tavily", tavily)
    ddgs.text.return_value = [source()]
    result = tool.execute(query="Kernersville NC weather", fetch_pages=False)
    assert result.success and result.metadata["engine"] == "duckduckgo"
    assert result.metadata["attempts"][0] == {
        "provider": "tavily",
        "status": "empty",
    }


def test_all_providers_fail_with_no_evidence_or_secret_echo(keyless):
    tool, ddgs, _ = keyless
    ddgs.text.side_effect = RuntimeError("secret-key-in-provider-error")
    result = tool.execute(query="Kernersville NC weather")
    assert not result.success
    assert result.metadata["evidence"]["records"] == []
    assert len(result.metadata["attempts"]) == 3
    assert "secret-key" not in str(result.metadata) + result.content
    assessment = assess_tool_results(
        detect_evidence_requirement("What is the weather forecast?"), [tool], [result]
    )
    assert assessment.blocked


def test_fetch_forecast_details_into_evidence(keyless, monkeypatch):
    tool, ddgs, _ = keyless
    ddgs.text.return_value = [source()]
    monkeypatch.setattr(web_search, "check_ssrf", lambda url: None)
    fetch = MagicMock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=(
                "<script>untrusted hidden text</script><h1>Kernersville</h1>"
                "<p>Today: Sunny. High 72 F.</p><p>Tonight: Low 51 F.</p>"
            ),
            request=httpx.Request("GET", source()["href"]),
        )
    )
    monkeypatch.setattr(web_search, "fetch_public_source", fetch)
    result = tool.execute(query="Kernersville NC weather")
    record = result.metadata["evidence"]["records"][0]
    assert "Today: Sunny. High 72 F.\nTonight: Low 51 F." in record["content"]
    assert "hidden text" not in record["content"]
    assert record["content_scope"] == "page_text"
    assert record["retrieved_at"] and record["url"] == source()["href"]
    assert fetch.call_args.kwargs["max_bytes"] == 2 * 1024 * 1024
    assert "deadline" in fetch.call_args.kwargs
    assert not assess_tool_results(
        detect_evidence_requirement("What is the weather forecast?"), [tool], [result]
    ).blocked


def test_blocked_page_retains_only_search_snippet(keyless, monkeypatch):
    tool, ddgs, _ = keyless
    ddgs.text.return_value = [source()]
    monkeypatch.setattr(web_search, "check_ssrf", lambda url: "blocked")
    fetch = MagicMock()
    monkeypatch.setattr(web_search, "fetch_public_source", fetch)
    result = tool.execute(query="Kernersville NC weather")
    fetch.assert_not_called()
    record = result.metadata["evidence"]["records"][0]
    assert record["content"] == source()["body"]
    assert "content_scope" not in record
    assert result.metadata["attempts"][-1]["status"] == "failed"


def test_page_reads_are_bounded(keyless, monkeypatch):
    tool, ddgs, _ = keyless
    ddgs.text.return_value = [
        dict(source(), href=f"https://example.test/{i}") for i in range(5)
    ]
    fetch = MagicMock(return_value="Fetched page text")
    monkeypatch.setattr(tool, "_fetch_url", fetch)
    result = tool.execute(query="Kernersville NC weather")
    assert result.success and fetch.call_count == 2


@pytest.mark.parametrize(
    "params",
    [
        {"max_results": 0},
        {"max_results": 1000},
        {"max_results": True},
        {"fetch_pages": "true"},
    ],
)
def test_invalid_parameters_do_not_search(keyless, params):
    tool, ddgs, _ = keyless
    assert not tool.execute(query="forecast", **params).success
    ddgs.text.assert_not_called()
