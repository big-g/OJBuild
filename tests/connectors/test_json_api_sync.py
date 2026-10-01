"""Bounded, same-origin pagination and durable upstream delta contracts."""

import json
from copy import deepcopy
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from openjarvis.connectors import web_sources
from openjarvis.connectors.source_manager import SourceManager
from openjarvis.connectors.source_store import SourceStore
from openjarvis.connectors.store import KnowledgeStore

BASE = "https://api.example.test/data?category=policy"


@pytest.fixture
def manager(tmp_path):
    return SourceManager(
        SourceStore(str(tmp_path / "sources.db"), legacy_path=str(tmp_path / "legacy")),
        knowledge_path=str(tmp_path / "knowledge.db"),
    )


@pytest.fixture
def fetch(monkeypatch):
    responses, calls = [], []

    def request(url, **kwargs):
        calls.append((url, deepcopy(kwargs)))
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return httpx.Response(
            200,
            content=json.dumps(item).encode(),
            headers={"content-type": "application/json"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(web_sources, "fetch_public_source", request)
    return responses, calls


def config(**kwargs):
    return {
        "url": BASE,
        "mode": "records",
        "records_pointer": "/items",
        "content_pointer": "/body",
        **kwargs,
    }


def page(ids, next_value=None, **kwargs):
    return {
        "items": [{"id": identity, "body": f"Policy {identity}"} for identity in ids],
        "next": next_value,
        **kwargs,
    }


def indexed(manager):
    with KnowledgeStore(manager.knowledge_path) as store:
        return {
            row["doc_id"]: row["content"]
            for row in store._conn.execute(
                "SELECT doc_id,content FROM knowledge_chunks"
            )
        }


@pytest.mark.parametrize("pagination", ["next_url", "cursor"])
def test_all_pages_keep_stable_record_ids_and_page_provenance(
    manager, fetch, pagination
):
    responses, calls = fetch
    responses.extend(
        [
            page(
                [1], "/data?page=2" if pagination == "next_url" else "opaque+cursor/2"
            ),
            page(["1"]),
        ]
    )
    connection = manager.create(
        "json_api",
        "Pages",
        config(pagination=pagination, next_pointer="/next", complete_snapshot=True),
    )
    assert manager.sync(connection["id"]) == 2
    assert len(calls) == 2
    if pagination == "cursor":
        assert parse_qs(urlparse(calls[1][0]).query) == {
            "category": ["policy"],
            "cursor": ["opaque+cursor/2"],
        }
    assert calls[0][1]["deadline"] == calls[1][1]["deadline"]
    with KnowledgeStore(manager.knowledge_path) as store:
        rows = store._conn.execute(
            "SELECT doc_id,metadata FROM knowledge_chunks"
        ).fetchall()
        assert len({row["doc_id"] for row in rows}) == 2  # int 1 != string "1"
        assert {json.loads(row["metadata"])["page_number"] for row in rows} == {1, 2}
    first_ids = set(indexed(manager))
    responses.extend(
        [
            page(["1"], "new-page" if pagination == "cursor" else "/different-page"),
            page([1]),
        ]
    )
    manager.sync(connection["id"])
    assert set(indexed(manager)) == first_ids


@pytest.mark.parametrize(
    "bad",
    [
        page(["a"], "https://other.example.test/data"),
        page(["a"], "http://api.example.test/data"),
        page(["a"], "https://127.0.0.1/data"),
        page(["a"], "https://user:password@api.example.test/data"),
        page(["a"], "https://api.example.test/data?token=secret"),
        page(["a"], 42),
    ],
)
def test_bad_next_url_fails_without_requesting_destination_or_ingesting(
    manager, fetch, bad
):
    responses, calls = fetch
    responses.append(bad)
    connection = manager.create(
        "json_api",
        "Pages",
        config(pagination="next_url", next_pointer="/next", complete_snapshot=True),
    )
    with pytest.raises(ValueError):
        manager.sync(connection["id"])
    assert len(calls) == 1
    assert indexed(manager) == {}
    assert manager.list()[0]["checkpoint"]["last_sync"] is None


@pytest.mark.parametrize(
    "pages,error",
    [
        ([page(["a"], "same"), page(["b"], "same")], "repeated"),
        ([page(["a"], "next"), page(["a"])], "duplicate"),
        ([page(["a"], "next"), ValueError("page failed")], "page failed"),
        ([page(["a"], "next"), {"items": []}], "does not exist"),
    ],
)
def test_failed_later_page_preserves_previous_index_and_watermark(
    manager, fetch, pages, error
):
    responses, _ = fetch
    connection = manager.create(
        "json_api",
        "Pages",
        config(pagination="cursor", next_pointer="/next", complete_snapshot=True),
    )
    responses.append(page(["old"]))
    manager.sync(connection["id"])
    before = manager.list()[0]["checkpoint"]
    old = indexed(manager)
    responses.extend(pages)
    with pytest.raises(ValueError, match=error):
        manager.sync(connection["id"])
    assert indexed(manager) == old
    checkpoint = manager.list()[0]["checkpoint"]
    assert checkpoint["last_sync"] == before["last_sync"]
    assert checkpoint["cursor"] == before["cursor"]


@pytest.mark.parametrize(
    "settings,pages,error",
    [
        ({"max_pages": 1}, [page(["a"], "more")], "page limit"),
        ({"max_records": 1}, [page(["a"], "more"), page(["b"])], "total record limit"),
    ],
)
def test_limits_fail_instead_of_returning_truncated_snapshot(
    manager, fetch, settings, pages, error
):
    responses, _ = fetch
    responses.extend(pages)
    connection = manager.create(
        "json_api",
        "Bounded",
        config(
            pagination="cursor",
            next_pointer="/next",
            complete_snapshot=True,
            **settings,
        ),
    )
    with pytest.raises(ValueError, match=error):
        manager.sync(connection["id"])
    assert indexed(manager) == {}


def test_complete_paginated_snapshot_removes_only_missing_records(manager, fetch):
    responses, _ = fetch
    connection = manager.create(
        "json_api",
        "Snapshot",
        config(pagination="cursor", next_pointer="/next", complete_snapshot=True),
    )
    responses.extend([page(["a"], "more"), page(["b"])])
    manager.sync(connection["id"])
    responses.extend([page([], "more"), page(["b"])])
    manager.sync(connection["id"])
    assert list(indexed(manager).values()) == ["Policy b"]


def delta_config(**kwargs):
    return config(
        sync_mode="incremental",
        sync_token_pointer="/sync_token",
        deleted_ids_pointer="/deleted",
        **kwargs,
    )


def test_delta_token_survives_restart_and_deletes_only_its_instance(manager, fetch):
    responses, calls = fetch
    one = manager.create("json_api", "One", delta_config())
    two = manager.create("json_api", "Two", config())
    responses.extend(
        [page(["a", "b"], sync_token="token+one/1", deleted=[]), page(["a"])]
    )
    manager.sync(one["id"])
    manager.sync(two["id"])
    assert len(indexed(manager)) == 3
    restarted = SourceManager(manager.store, knowledge_path=manager.knowledge_path)
    responses.append(page(["c"], sync_token="token-two", deleted=["a"]))
    restarted.sync(one["id"])
    assert parse_qs(urlparse(calls[-1][0]).query)["since"] == ["token+one/1"]
    assert parse_qs(urlparse(calls[-1][0]).query)["category"] == ["policy"]
    rows = indexed(manager)
    assert len(rows) == 3
    assert (
        sum(value == "Policy a" for value in rows.values()) == 1
    )  # other instance retained
    assert "Policy b" in rows.values()  # absent from delta != deleted
    checkpoint = next(item for item in manager.list() if item["id"] == one["id"])[
        "checkpoint"
    ]
    assert json.loads(checkpoint["cursor"])["token"] == "token-two"
    # An empty successful delta still advances the durable token.
    responses.append(page([], sync_token="empty-token", deleted=[]))
    restarted.sync(one["id"])
    checkpoint = next(item for item in manager.list() if item["id"] == one["id"])[
        "checkpoint"
    ]
    assert json.loads(checkpoint["cursor"])["token"] == "empty-token"


def test_delta_failure_retries_same_token_and_never_applies_deletions(manager, fetch):
    responses, calls = fetch
    connection = manager.create(
        "json_api", "Delta", delta_config(pagination="cursor", next_pointer="/next")
    )
    responses.append(page(["old"], sync_token="old-token", deleted=[]))
    manager.sync(connection["id"])
    before = manager.list()[0]["checkpoint"]
    responses.extend(
        [page(["new"], "page2", deleted=["old"]), ValueError("failed page")]
    )
    with pytest.raises(ValueError):
        manager.sync(connection["id"])
    after = manager.list()[0]["checkpoint"]
    assert after["cursor"] == before["cursor"]
    assert after["last_sync"] == before["last_sync"]
    assert list(indexed(manager).values()) == ["Policy old"]
    responses.append(page(["new"], sync_token="new-token", deleted=["old"]))
    manager.sync(connection["id"])
    assert parse_qs(urlparse(calls[-1][0]).query)["since"] == ["old-token"]
    assert list(indexed(manager).values()) == ["Policy new"]


def test_cleanup_failure_does_not_advance_upstream_token(manager, fetch, monkeypatch):
    responses, calls = fetch
    connection = manager.create("json_api", "Delta", delta_config())
    responses.append(page(["old"], sync_token="old-token", deleted=[]))
    manager.sync(connection["id"])
    before = manager.list()[0]["checkpoint"]
    original = KnowledgeStore.delete_documents

    def fail(*args):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(KnowledgeStore, "delete_documents", fail)
    responses.append(page([], sync_token="new-token", deleted=["old"]))
    with pytest.raises(RuntimeError):
        manager.sync(connection["id"])
    after = manager.list()[0]["checkpoint"]
    assert after["cursor"] == before["cursor"]
    assert after["last_sync"] == before["last_sync"]
    assert list(indexed(manager).values()) == ["Policy old"]
    monkeypatch.setattr(KnowledgeStore, "delete_documents", original)
    responses.append(page([], sync_token="new-token", deleted=["old"]))
    manager.sync(connection["id"])
    assert indexed(manager) == {}


@pytest.mark.parametrize(
    "extra",
    [
        {"sync_token": "", "deleted": []},
        {"sync_token": None, "deleted": []},
        {"sync_token": "token", "deleted": [True]},
        {"sync_token": "token", "deleted": {}},
        {"sync_token": "token", "deleted": ["a"]},
    ],
)
def test_invalid_delta_contract_fails_before_yielding(fetch, extra):
    responses, _ = fetch
    responses.append(page(["a"], **extra))
    with pytest.raises(ValueError):
        next(web_sources.JsonAPIConnector(config=delta_config()).sync())


@pytest.mark.parametrize(
    "settings",
    [
        {"mode": "document", "pagination": "cursor", "next_pointer": "/next"},
        {"pagination": "cursor"},
        {"max_pages": True},
        {"max_pages": 51},
        {"pagination": "other"},
        {"cursor_parameter": "access_token"},
        {"sync_mode": "incremental"},
        {
            "sync_mode": "incremental",
            "sync_token_pointer": "/sync",
            "complete_snapshot": True,
        },
        {"deleted_ids_pointer": "/deleted"},
        {
            "sync_mode": "incremental",
            "sync_token_pointer": "/sync",
            "pagination": "cursor",
            "next_pointer": "/next",
            "cursor_parameter": "since",
        },
    ],
)
def test_invalid_contract_configuration_is_rejected(settings):
    with pytest.raises(ValueError):
        web_sources.validate_json_config(config(**settings))


def test_test_connection_scans_without_saving_token_or_deleting_index(manager, fetch):
    responses, _ = fetch
    connection = manager.create("json_api", "Existing", config())
    responses.append(page(["old"]))
    manager.sync(connection["id"])
    before = manager.list()[0]["checkpoint"]
    responses.append(page([], sync_token="test-token", deleted=["old"]))
    assert manager.test("json_api", delta_config())["documents"] == 0
    assert list(indexed(manager).values()) == ["Policy old"]
    assert manager.list()[0]["checkpoint"] == before


def test_authenticated_continuations_keep_header_and_reject_secret_tokens(
    manager, fetch
):
    responses, calls = fetch
    credential = manager.credentials.create(
        "Token", "bearer", "https://api.example.test", "protected-token-long"
    )
    connection = manager.create(
        "json_api",
        "Auth",
        delta_config(
            credential_id=credential["id"], pagination="cursor", next_pointer="/next"
        ),
    )
    responses.extend(
        [page(["a"], "next", deleted=[]), page([], sync_token="safe-token", deleted=[])]
    )
    manager.sync(connection["id"])
    assert len(calls) == 2
    assert [call[1]["authentication"]["headers"] for call in calls] == [
        {"Authorization": "Bearer protected-token-long"}
    ] * 2
    before = manager.list()[0]["checkpoint"]
    responses.append(page([], sync_token="protected-token-long", deleted=[]))
    with pytest.raises(ValueError) as error:
        manager.sync(connection["id"])
    assert "protected-token-long" not in str(error.value)
    assert manager.list()[0]["checkpoint"]["cursor"] == before["cursor"]


def test_total_page_bytes_are_bounded_before_ingestion(manager, fetch):
    responses, _ = fetch
    for number in range(6):
        responses.append(
            {
                "items": [{"id": number, "body": "x" * 1800000}],
                "next": str(number + 1) if number < 5 else None,
            }
        )
    connection = manager.create(
        "json_api", "Bytes", config(pagination="cursor", next_pointer="/next")
    )
    with pytest.raises(ValueError, match="total response limit"):
        manager.sync(connection["id"])
    assert indexed(manager) == {}


def test_explicit_deletion_failure_rolls_back_chunks_and_fts(manager, fetch):
    responses, _ = fetch
    connection = manager.create("json_api", "Rollback", config())
    responses.append(page(["a", "b"]))
    manager.sync(connection["id"])
    before = indexed(manager)
    with KnowledgeStore(manager.knowledge_path) as store:
        store._conn.execute("""CREATE TEMP TRIGGER reject_b BEFORE DELETE
            ON knowledge_chunks WHEN old.content='Policy b'
            BEGIN SELECT RAISE(ABORT, 'cleanup failure'); END""")
        with pytest.raises(Exception, match="cleanup failure"):
            store.delete_documents(before)
        assert store.count() == 2
        assert (
            store._conn.execute(
                "SELECT count(*) FROM knowledge_fts WHERE knowledge_fts MATCH ?",
                ("Policy",),
            ).fetchone()[0]
            == 2
        )


def test_invalid_checkpoint_token_fails_before_network_request(fetch):
    _, calls = fetch
    with pytest.raises(ValueError, match="Stored API sync token"):
        list(
            web_sources.JsonAPIConnector(config=delta_config()).sync(
                cursor='{"token":"ignored"}'
            )
        )
    assert calls == []


def test_ingestion_failure_preserves_token_and_defers_deletions(
    manager, fetch, monkeypatch
):
    from openjarvis.connectors.pipeline import IngestionPipeline

    responses, _ = fetch
    connection = manager.create("json_api", "Ingest", delta_config())
    responses.append(page(["old"], sync_token="old-token", deleted=[]))
    manager.sync(connection["id"])
    before = manager.list()[0]["checkpoint"]

    def fail(*args):
        raise RuntimeError("ingestion failed")

    monkeypatch.setattr(IngestionPipeline, "ingest", fail)
    responses.append(page(["new"], sync_token="new-token", deleted=["old"]))
    with pytest.raises(RuntimeError, match="ingestion failed"):
        manager.sync(connection["id"])
    assert manager.list()[0]["checkpoint"]["cursor"] == before["cursor"]
    assert manager.list()[0]["checkpoint"]["last_sync"] == before["last_sync"]
    assert list(indexed(manager).values()) == ["Policy old"]
