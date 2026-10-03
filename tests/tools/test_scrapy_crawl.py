"""Tests for the optional bounded Scrapy website crawler."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from openjarvis.core.evidence import EvidenceKind, evidence_records_from_tool_result
from openjarvis.tools.scrapy_crawl import ScrapyCrawlTool


def test_spec_declares_optional_crawler_and_evidence_support() -> None:
    spec = ScrapyCrawlTool().spec

    assert spec.name == "web_crawl"
    assert spec.category == "search"
    assert spec.timeout_seconds > 60
    assert set(spec.evidence_kinds) == {
        EvidenceKind.CURRENT.value,
        EvidenceKind.EXTERNAL.value,
    }
    assert spec.metadata["install_extra"] == "tools-crawl"
    assert spec.metadata["same_host_only"] is True


def test_execute_requires_a_url() -> None:
    result = ScrapyCrawlTool().execute()

    assert not result.success
    assert result.content == "No URL provided."


def test_execute_rejects_private_targets_before_starting_worker() -> None:
    with patch("openjarvis.tools.scrapy_crawl.run_worker") as run:
        result = ScrapyCrawlTool().execute(url="http://127.0.0.1/private")

    assert not result.success
    assert "blocked" in result.content.lower()
    run.assert_not_called()


def test_execute_rejects_malformed_urls_without_starting_worker() -> None:
    with patch("openjarvis.tools.scrapy_crawl.run_worker") as run:
        result = ScrapyCrawlTool().execute(url="http://[invalid")

    assert not result.success
    assert "valid public" in result.content.lower()
    run.assert_not_called()


def test_execute_reports_missing_optional_dependency(monkeypatch) -> None:
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: None if name == "scrapy" else object(),
    )

    result = ScrapyCrawlTool().execute(url="https://example.com")

    assert not result.success
    assert "uv sync --extra tools-crawl" in result.content


def test_execute_returns_page_provenance_and_evidence(monkeypatch) -> None:
    page = {
        "title": "Example Guide",
        "url": "https://example.com/guide",
        "content": "A useful fact from this page.",
        "depth": 1,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }
    completed = {"pages": [page]}
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name == "scrapy" else None,
    )
    with patch(
        "openjarvis.tools.scrapy_crawl.run_worker", return_value=completed
    ) as run:
        result = ScrapyCrawlTool().execute(
            url="https://example.com/start",
            max_pages=99,
        )

    assert result.success
    assert "https://example.com/guide" in result.content
    assert result.metadata["provider"] == "scrapy"
    assert result.metadata["pages"] == 1
    assert result.metadata["evidence"]["records"][0]["source_id"] == page["url"]
    assert (
        evidence_records_from_tool_result(
            tool_name="web_crawl",
            metadata=result.metadata,
        )[0].url
        == page["url"]
    )
    args, payload = run.call_args.args
    assert args == "openjarvis.tools.scrapy_worker"
    assert payload["max_pages"] == 8


def test_execute_fails_closed_when_worker_returns_invalid_json(monkeypatch) -> None:
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name == "scrapy" else None,
    )
    with patch(
        "openjarvis.tools.scrapy_crawl.run_worker",
        side_effect=ValueError("invalid output"),
    ):
        result = ScrapyCrawlTool().execute(url="https://example.com")

    assert not result.success
    assert "crawl failed" in result.content
