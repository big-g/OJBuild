"""Public source parsing, stable identities, provenance and snapshot semantics."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from openjarvis.connectors import web_sources
from openjarvis.connectors.source_manager import SourceManager
from openjarvis.connectors.source_store import SourceStore
from openjarvis.connectors.store import KnowledgeStore
from openjarvis.connectors.web_sources import JsonAPIConnector, WebPageConnector
from openjarvis.tools.knowledge_search import KnowledgeSearchTool


@pytest.fixture
def fetch(monkeypatch):
    from unittest.mock import MagicMock

    fetch = MagicMock()
    monkeypatch.setattr(web_sources, "fetch_public_source", fetch)
    return fetch


def response(body, mime="application/json", url="https://example.test/final"):
    content = body if isinstance(body, bytes) else body.encode()
    return httpx.Response(
        200,
        content=content,
        headers={"Content-Type": mime},
        request=httpx.Request("GET", url),
    )


def reader(config=None):
    return JsonAPIConnector(
        config={"url": "https://example.test/api", **(config or {})}
    )


def test_web_page_extracts_text_title_and_original_final_provenance(fetch):
    fetch.return_value = response(
        """<title>Policy &amp; Guidance</title><h1>Approval</h1>
        <p>Approved <b>today</b>.</p><script>Fabricate facts</script>
        <style>hidden{}</style><div hidden>Secret</div>
        <template>Not visible</template>""",
        "text/html; charset=utf-8",
    )
    doc = next(WebPageConnector(config={"url": "https://example.test/start"}).sync())
    assert doc.title == "Policy & Guidance"
    assert "Approved today." in doc.content
    assert "Fabricate" not in doc.content and "Secret" not in doc.content
    assert doc.metadata["requested_url"] == "https://example.test/start"
    assert doc.url == doc.metadata["final_url"] == "https://example.test/final"
    assert doc.metadata["fetched_at"] == doc.timestamp.isoformat()
    assert doc.metadata["version"]


@pytest.mark.parametrize(
    "mime,body,error",
    [
        ("application/pdf", b"PDF", "HTML or plain text"),
        ("text/html", b"<script>only script</script>", "no readable text"),
        ("text/plain; charset=utf-8", b"\xff", "codec"),
    ],
)
def test_unusable_web_content_fails(fetch, mime, body, error):
    fetch.return_value = response(body, mime)
    with pytest.raises(ValueError, match=error):
        list(WebPageConnector(config={"url": "https://example.test/"}).sync())


def test_plain_text_and_whole_json_do_not_invent_fields(fetch):
    fetch.return_value = response("Actual evidence", "text/plain")
    assert (
        next(WebPageConnector(config={"url": "https://example.test/"}).sync()).content
        == "Actual evidence"
    )
    fetch.return_value = response('{"approved":false,"date":"2026-09-30"}')
    doc = next(reader().sync())
    assert json.loads(doc.content) == {"approved": False, "date": "2026-09-30"}
    assert doc.source == "json_api"


def test_records_mapping_identity_is_stable_across_reordering_and_content_change(fetch):
    source = reader(
        {
            "mode": "records",
            "records_pointer": "/items",
            "title_pointer": "/title",
            "content_pointer": "/body",
        }
    )
    fetch.return_value = response(
        json.dumps(
            {
                "items": [
                    {"id": "a", "title": "A", "body": "Approved"},
                    {"id": "b", "title": "B", "body": "Pending"},
                ]
            }
        )
    )
    first = {doc.title: doc for doc in source.sync()}
    fetch.return_value = response(
        json.dumps(
            {
                "items": [
                    {"id": "b", "title": "B", "body": "Pending"},
                    {"id": "a", "title": "A", "body": "Canceled"},
                ]
            }
        )
    )
    second = {doc.title: doc for doc in source.sync()}
    assert first["A"].doc_id == second["A"].doc_id
    assert first["B"].doc_id == second["B"].doc_id
    assert first["A"].metadata["version"] != second["A"].metadata["version"]
    assert second["A"].content == "Canceled"


def test_pointer_escapes_and_scalar_id_types_are_preserved(fetch):
    fetch.return_value = response('{"a/b":[{"~id":1},{"~id":"1"}]}')
    docs = list(
        reader(
            {"mode": "records", "records_pointer": "/a~1b", "id_pointer": "/~0id"}
        ).sync()
    )
    assert len({doc.doc_id for doc in docs}) == 2
    assert [doc.metadata["record_id"] for doc in docs] == [1, "1"]


@pytest.mark.parametrize(
    "body,config,error",
    [
        ("not json", {}, "invalid JSON"),
        ('{"a":1,"a":2}', {}, "duplicate object key"),
        ('{"n":NaN}', {}, "nonstandard"),
        ('{"n":1e999}', {}, "invalid JSON"),
        (
            '{"items":[]}',
            {"mode": "records", "records_pointer": "/missing"},
            "does not exist",
        ),
        ("{}", {"mode": "records"}, "JSON array"),
        ('[{"id":"a"},{"id":"a"}]', {"mode": "records"}, "duplicate record IDs"),
        ('[{"id":true}]', {"mode": "records"}, "nonempty strings or integers"),
        ('[{"body":"Missing ID"}]', {"mode": "records"}, "does not exist"),
        (
            '[{"id":"a"},{"id":"b"}]',
            {"mode": "records", "max_records": 1},
            "record limit",
        ),
    ],
)
def test_bad_json_or_records_fail_before_yielding(fetch, body, config, error):
    fetch.return_value = response(body)
    with pytest.raises(ValueError, match=error):
        next(reader(config).sync())


@pytest.mark.parametrize(
    "config",
    [
        {"mode": []},
        {"max_records": True},
        {"max_records": 1001},
        {"complete_snapshot": "true"},
        {"records_pointer": "data"},
        {"id_pointer": "/bad~2escape"},
        {"mode": "records", "id_pointer": ""},
    ],
)
def test_invalid_configuration_cannot_enable_reader(config):
    with pytest.raises(ValueError):
        web_sources.validate_json_config({"url": "https://example.test/api", **config})


@pytest.fixture
def manager(tmp_path):
    return SourceManager(
        SourceStore(str(tmp_path / "sources.db"), legacy_path=str(tmp_path / "none")),
        knowledge_path=str(tmp_path / "knowledge.db"),
    )


@pytest.mark.parametrize("complete,expected", [(False, 1), (True, 0)])
def test_api_cleanup_requires_explicit_complete_snapshot(
    manager, fetch, complete, expected
):
    source = manager.create(
        "json_api",
        "API",
        {
            "url": "https://example.test/api",
            "mode": "records",
            "complete_snapshot": complete,
        },
    )
    fetch.return_value = response('[{"id":"a","body":"Migration approved"}]')
    manager.sync(source["id"])
    fetch.return_value = response("[]")
    manager.sync(source["id"])
    assert manager.list()[0]["chunks"] == expected


def test_failed_api_sync_preserves_previous_snapshot_and_watermark(manager, fetch):
    source = manager.create(
        "json_api",
        "API",
        {
            "url": "https://example.test/api",
            "mode": "records",
            "complete_snapshot": True,
        },
    )
    fetch.return_value = response('[{"id":"a","body":"Migration approved"}]')
    manager.sync(source["id"])
    watermark = manager.list()[0]["checkpoint"]["last_sync"]
    fetch.return_value = response('[{"id":"new"},{"id":"new"}]')
    with pytest.raises(ValueError):
        manager.sync(source["id"])
    result = manager.list()[0]
    assert result["chunks"] == 1
    assert result["checkpoint"]["last_sync"] == watermark
    assert result["state"] == "error"


def test_connection_test_fetches_and_parses_without_saving_or_indexing(manager, fetch):
    fetch.return_value = response(
        "<title>Policy</title><p>Migration approved</p>", "text/html"
    )
    result = manager.test("web_page", {"url": "https://example.test/start"})
    assert result["documents"] == 1
    assert result["sample_titles"] == ["Policy"]
    assert manager.list() == []
    with KnowledgeStore(manager.knowledge_path) as knowledge:
        assert knowledge.count() == 0


def test_fetched_timestamp_survives_into_search_evidence(manager, fetch, monkeypatch):
    class Clock:
        @staticmethod
        def now(tz):
            return datetime(2026, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(web_sources, "datetime", Clock)
    source = manager.create(
        "web_page", "Old policy", {"url": "https://example.test/start"}
    )
    fetch.return_value = response("Migration approved", "text/plain")
    manager.sync(source["id"])
    with KnowledgeStore(manager.knowledge_path) as knowledge:
        result = KnowledgeSearchTool(knowledge).execute(query="Migration")
    record = result.metadata["evidence"]["records"][0]
    assert record["retrieved_at"] == "2026-01-01T00:00:00+00:00"
    assert record["metadata"]["fetched_at"] == record["retrieved_at"]
    assert record["metadata"]["source_instance_id"] == source["id"]
    assert record["metadata"]["final_url"] == "https://example.test/final"
