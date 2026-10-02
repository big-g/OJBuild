"""Mock-only named Notion readers: vault binding, bounds and instance isolation."""

import json
from dataclasses import replace
from unittest.mock import MagicMock

import httpx
import pytest

from openjarvis.connectors import notion_sources, source_adapters
from openjarvis.connectors.source_manager import SourceManager
from openjarvis.connectors.source_store import SourceConflict, SourceStore
from openjarvis.connectors.store import KnowledgeStore
from openjarvis.connectors.sync_control import SyncCancelled

PAGE = "00000000-0000-4000-8000-000000000001"
BLOCK = "00000000-0000-4000-8000-000000000002"
CHILD = "00000000-0000-4000-8000-000000000003"
TOKEN = "notion-protected-example-token"


def page(identity=PAGE):
    return {
        "object": "page",
        "id": identity,
        "last_edited_time": "2026-09-30T12:00:00Z",
        "properties": {"Title": {"title": [{"plain_text": "Policy"}]}},
    }


def block(identity=BLOCK, text="Approved policy", children=False):
    return {
        "object": "block",
        "id": identity,
        "type": "paragraph",
        "has_children": children,
        "paragraph": {"rich_text": [{"plain_text": text}]},
    }


def response(results, more=False, cursor=None):
    return httpx.Response(
        200,
        json={
            "results": results,
            "has_more": more,
            "next_cursor": cursor,
        },
    )


@pytest.fixture
def manager(tmp_path):
    return SourceManager(
        SourceStore(
            str(tmp_path / "sources.db"), legacy_path=str(tmp_path / "missing")
        ),
        knowledge_path=str(tmp_path / "knowledge.db"),
    )


@pytest.fixture
def fetch(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("Named Notion tests must never reach live services")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    fetch = MagicMock(side_effect=[response([page()]), response([block()])])
    monkeypatch.setattr(notion_sources, "fetch_public_source", fetch)
    return fetch


def config(manager, secret=TOKEN, **settings):
    credential = manager.credentials.create(
        "Notion integration",
        "bearer",
        notion_sources.NOTION_ORIGIN,
        secret,
    )
    return {"credential_id": credential["id"], **settings}


def test_named_connections_use_separate_credentials_documents_and_checkpoints(
    manager, fetch
):
    one = manager.create("notion_pages", "Work", config(manager))
    two = manager.create("notion_pages", "Home", config(manager, "separate-home-token"))
    fetch.side_effect = [response([page()]), response([block()])] * 3
    manager.sync(one["id"])
    manager.sync(two["id"])
    calls = fetch.call_args_list
    assert calls[0].kwargs["authentication"]["secret"] == TOKEN
    assert calls[2].kwargs["authentication"]["secret"] == "separate-home-token"
    with KnowledgeStore(manager.knowledge_path) as knowledge:
        assert knowledge.count_document_prefix(f"source:{one['id']}:") == 1
        assert knowledge.count_document_prefix(f"source:{two['id']}:") == 1
    assert all(record["checkpoint"]["last_sync"] for record in manager.list())
    manager.delete(one["id"], 1)
    with KnowledgeStore(manager.knowledge_path) as knowledge:
        assert knowledge.count_document_prefix(f"source:{one['id']}:") == 0
        assert knowledge.count_document_prefix(f"source:{two['id']}:") == 1
    assert TOKEN not in manager.store.path.read_bytes().decode(errors="replace")


def test_probe_uses_search_without_indexing_and_clears_ephemeral_material(
    manager, fetch, monkeypatch
):
    readers = []
    original = source_adapters.get_adapter("notion_pages")

    def factory(config):
        reader = notion_sources.NotionSource(config=config)
        readers.append(reader)
        return reader

    monkeypatch.setitem(
        source_adapters._ADAPTERS, "notion_pages", replace(original, factory=factory)
    )
    values = config(manager)
    result = manager.test("notion_pages", values)
    assert result["ok"] and result["documents"] == 1
    assert manager.list() == []
    assert TOKEN not in json.dumps(result)
    assert readers[0]._authentication is None
    assert fetch.call_count == 1
    (url,) = fetch.call_args.args
    assert url == "https://api.notion.com/v1/search"
    assert fetch.call_args.kwargs["method"] == "POST"
    assert json.loads(fetch.call_args.kwargs["body"])["page_size"] == 1
    assert (
        fetch.call_args.kwargs["authentication"]["headers"]["Notion-Version"]
        == "2022-06-28"
    )


def test_search_and_nested_blocks_paginate_with_one_shared_budget(manager, fetch):
    values = config(manager, query="Policy")
    fetch.side_effect = [
        response([page()], True, "search-cursor"),
        response([block(children=True)], True, "blocks-cursor"),
        response([block(CHILD, "Nested clause")]),
        response([block("00000000-0000-4000-8000-000000000004", "Last clause")]),
        response([]),
    ]
    with manager._reading(manager.create("notion_pages", "Policies", values)) as source:
        docs = list(source.sync())
    assert len(docs) == 1
    assert docs[0].content == "Approved policy\nNested clause\nLast clause"
    assert docs[0].metadata["provider_modified_at"] == "2026-09-30T12:00:00Z"
    assert docs[0].metadata["fetched_at"]
    assert docs[0].metadata["source_instance_name"] == "Policies"
    calls = fetch.call_args_list
    assert "start_cursor=blocks-cursor" in calls[3].args[0]
    assert json.loads(calls[4].kwargs["body"])["start_cursor"] == "search-cursor"
    assert json.loads(calls[0].kwargs["body"])["query"] == "Policy"
    assert len({call.kwargs["deadline"] for call in calls}) == 1


@pytest.mark.parametrize(
    "limit",
    [
        {"max_requests": 1},
        {"max_blocks": 1},
        {"max_pages": 1},
    ],
)
def test_scan_limits_fail_without_indexing_or_advancing_checkpoint(
    manager, fetch, limit
):
    source = manager.create("notion_pages", "Limited", config(manager, **limit))
    fetch.side_effect = [
        response([page(), page(CHILD)]),
        response([block(), block(CHILD)]),
    ]
    with pytest.raises(ValueError, match="Authenticated source"):
        manager.sync(source["id"])
    record = manager.list()[0]
    assert record["chunks"] == 0
    assert record["checkpoint"]["last_sync"] is None
    assert TOKEN not in record["error"]


@pytest.mark.parametrize(
    "kind,origin",
    [
        ("bearer", "https://wrong.example"),
        ("api_key", "https://api.notion.com"),
    ],
)
def test_credentials_must_match_fixed_provider_origin_and_kind(manager, kind, origin):
    credential = manager.credentials.create("Wrong", kind, origin, TOKEN, "X-API-Key")
    with pytest.raises(ValueError, match="match"):
        manager.create("notion_pages", "Wrong", {"credential_id": credential["id"]})
    assert manager.list() == []


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"credential_id": "not-a-uuid"},
        {"credential_id": PAGE, "max_pages": True},
        {"credential_id": PAGE, "max_requests": 1001},
        {"credential_id": PAGE, "query": "x" * 201},
        {"credential_id": PAGE, "url": "https://attacker.example"},
        {"credential_id": PAGE, "token": TOKEN},
    ],
)
def test_invalid_config_and_secret_fields_are_rejected(manager, values):
    with pytest.raises(ValueError):
        manager.create("notion_pages", "Invalid", values)
    assert manager.list() == []


@pytest.mark.parametrize(
    "bad",
    [
        httpx.Response(200, json={"results": [], "has_more": "false"}),
        response([page("../../attacker")]),
        httpx.Response(200, content=b"invalid-json"),
        httpx.Response(200, json={"results": TOKEN}),
        response([page()], True, ""),
    ],
)
def test_malformed_provider_responses_fail_without_leaking_values(manager, fetch, bad):
    fetch.side_effect = [bad, response([block()])]
    with pytest.raises(ValueError) as error:
        manager.sync(manager.create("notion_pages", "Bad", config(manager))["id"])
    assert TOKEN not in str(error.value)
    assert manager.list()[0]["chunks"] == 0


def test_cursor_cycles_fail_with_no_success_checkpoint(manager, fetch):
    fetch.side_effect = [response([], True, "repeat"), response([], True, "repeat")]
    with pytest.raises(ValueError):
        manager.sync(manager.create("notion_pages", "Cycle", config(manager))["id"])
    assert fetch.call_count == 2


def test_secret_reflections_are_not_indexed_or_returned_by_probe(manager, fetch):
    fetch.side_effect = [response([page()]), response([block(text=TOKEN)])]
    source = manager.create("notion_pages", "Protected", config(manager))
    with pytest.raises(ValueError):
        manager.sync(source["id"])
    fetch.side_effect = [response([{"title": TOKEN}])]
    with pytest.raises(ValueError) as error:
        manager.test("notion_pages", source["config"])
    assert TOKEN not in str(error.value)


def test_missing_pages_do_not_reconcile_an_incomplete_search_inventory(manager, fetch):
    source = manager.create("notion_pages", "Kept", config(manager))
    manager.sync(source["id"])
    fetch.side_effect = [response([])]
    manager.sync(source["id"])
    assert manager.list()[0]["chunks"] == 1


def test_rotation_and_deletion_are_blocked_during_a_named_source_read(manager, fetch):
    source = manager.create("notion_pages", "Locked", config(manager))
    credential = manager.credentials.list()[0]
    with manager._reading(source):
        with pytest.raises(SourceConflict):
            manager.credentials.rotate(credential["id"], 1, "replacement-token")
        with pytest.raises(SourceConflict):
            manager.delete_credential(credential["id"], 1)
    with pytest.raises(SourceConflict):
        manager.delete_credential(credential["id"], 1)


def test_cancellation_propagates_without_becoming_a_parser_failure(manager, fetch):
    source = manager.create("notion_pages", "Cancelled", config(manager))
    control = MagicMock()
    control.check.side_effect = SyncCancelled()
    with manager._reading(source, control=control) as reader:
        with pytest.raises(SyncCancelled):
            list(reader.sync())
    fetch.assert_not_called()


def test_agent_reads_require_explicit_provider_network_and_credential_capabilities():
    from openjarvis.tools.digest_collect import DigestCollectTool

    tool = DigestCollectTool()
    required = tool.resolve_required_capabilities({"sources": ["notion_pages"]})
    assert set(required) == {"connector:notion:read", "network:fetch", "credential:use"}
    assert "credential:use" not in tool.resolve_required_capabilities(
        {"sources": ["notion"]}
    )


def test_disable_and_remove_keep_other_instance_reads_independent(manager, fetch):
    one = manager.create("notion_pages", "One", config(manager))
    two = manager.create(
        "notion_pages", "Two", config(manager, "second-protected-token")
    )
    manager.update(one["id"], 1, name="One", config=one["config"], enabled=False)
    documents = list(manager.collect("notion_pages"))
    assert len(documents) == 1
    assert documents[0].metadata["source_instance_id"] == two["id"]
    assert fetch.call_count == 2


def test_mid_fetch_cancellation_does_not_yield_documents(manager, fetch):
    source = manager.create("notion_pages", "Cancelled", config(manager))
    control = MagicMock()

    def cancel_after_fetch(*args, **kwargs):
        control.check.side_effect = SyncCancelled()
        return response([page()])

    fetch.side_effect = cancel_after_fetch
    with manager._reading(source, control=control) as reader:
        with pytest.raises(SyncCancelled):
            list(reader.sync())
    assert fetch.call_count == 1


def test_response_and_total_byte_limits_fail_before_indexing(manager, fetch):
    source = manager.create("notion_pages", "Bounded", config(manager))
    fetch.side_effect = [httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))]
    with pytest.raises(ValueError):
        manager.sync(source["id"])
    with manager._reading(source) as reader:
        reader.reader._begin()
        reader.reader._bytes = 16 * 1024 * 1024
        fetch.side_effect = [response([])]
        with pytest.raises(ValueError, match="response byte limit"):
            reader.reader._request("/search", body={})
    assert manager.list()[0]["chunks"] == 0


def test_nested_block_cycles_and_duplicate_pages_are_rejected(manager, fetch):
    source = manager.create("notion_pages", "Cycle", config(manager))
    fetch.side_effect = [response([page()]), response([block(PAGE, children=True)])]
    with pytest.raises(ValueError):
        manager.sync(source["id"])
    fetch.side_effect = [response([page(), page()]), response([block()])]
    with pytest.raises(ValueError):
        manager.sync(source["id"])
    assert manager.list()[0]["chunks"] == 0


def test_missing_vault_key_prevents_any_provider_request(manager, fetch):
    source = manager.create("notion_pages", "Protected", config(manager))
    manager.credentials.key_path.unlink()
    with pytest.raises(ValueError, match="key is missing"):
        manager.sync(source["id"])
    fetch.assert_not_called()


def test_block_nesting_limit_is_enforced_before_more_fetches(manager, fetch):
    source = manager.create("notion_pages", "Nested", config(manager))
    fetch.side_effect = [response([page()])] + [
        response([block(f"00000000-0000-4000-8000-{index:012d}", children=True)])
        for index in range(10, 20)
    ]
    with pytest.raises(ValueError):
        manager.sync(source["id"])
    assert fetch.call_count == 10
    assert manager.list()[0]["chunks"] == 0
