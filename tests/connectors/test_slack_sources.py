"""Strict named Slack scans: synthetic provider data, no network or accounts."""

import json
import socket
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from openjarvis.connectors import slack_sources as module
from openjarvis.connectors.instance_sources import (
    AccountSource,
    token_path,
    validate_config,
)
from openjarvis.connectors.oauth import save_tokens
from openjarvis.connectors.pipeline import IngestionPipeline
from openjarvis.connectors.store import KnowledgeStore
from openjarvis.connectors.sync_control import SyncCancelled
from openjarvis.connectors.sync_engine import SyncEngine

TOKEN = "xoxp-protected-synthetic-slack-value"
AUTH = {
    "ok": True,
    "team_id": "T123",
    "user_id": "U123",
    "url": "https://example.slack.com/",
}
USER = {"id": "U123", "real_name": "Alice", "profile": {"email": "alice@example.org"}}
CHANNEL = {"id": "C123", "name": "research", "is_archived": True}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Live provider traffic forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)


def reader(tmp_path, config=None):
    import uuid

    identity = str(uuid.uuid4())
    save_tokens(str(token_path(tmp_path, identity)), {"token": TOKEN})
    return AccountSource("slack", {"id": identity, "config": config or {}}, tmp_path)


def page(field, items, cursor="", more=False):
    value = {"ok": True, field: items, "response_metadata": {"next_cursor": cursor}}
    if field == "messages":
        value["has_more"] = more
    return value


def message(ts="1500000000.000001", text="Older research", **extra):
    return {"type": "message", "user": "U123", "ts": ts, "text": text, **extra}


def prefix(channels=None):
    return [
        AUTH,
        page("members", [USER]),
        page("channels", channels if channels is not None else [CHANNEL]),
    ]


def responses(monkeypatch, pages):
    calls = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        payload = pages[len(calls) - 1]
        if callable(payload):
            payload = payload(url)
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, httpx.Response):
            return payload
        return httpx.Response(
            200,
            content=payload
            if isinstance(payload, bytes)
            else json.dumps(payload).encode(),
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(module, "fetch_public_source", fetch)
    return calls


def test_all_collections_and_threads_are_paginated(tmp_path, monkeypatch):
    connector = reader(tmp_path)
    root = message(thread_ts="1500000000.000001", reply_count=2)
    calls = responses(
        monkeypatch,
        [
            AUTH,
            page("members", [], "user-cursor"),
            page("members", [USER]),
            page("channels", [], "channel-cursor"),
            page("channels", [CHANNEL]),
            page("messages", [root], "history-cursor", False),
            page("messages", [message("1400000000.000001", "Even older history")]),
            page(
                "messages",
                [
                    root,
                    message("1500000001.000001", "First reply", thread_ts=root["ts"]),
                ],
                "reply-cursor",
                True,
            ),
            page(
                "messages",
                [message("1500000002.000001", "Second reply", thread_ts=root["ts"])],
            ),
        ],
    )
    docs = list(
        connector.sync(
            since=datetime(2026, 10, 1, tzinfo=timezone.utc), cursor="previous"
        )
    )
    assert len(docs) == 4
    assert len({doc.source_id for doc in docs}) == 4
    assert docs[0].author == "alice@example.org"
    assert docs[0].url == "https://example.slack.com/archives/C123/p1500000000000001"
    assert docs[-1].thread_id == root["ts"]
    assert all(
        doc.metadata["coverage"]
        == "provider_available_conversation_history_and_threads"
        for doc in docs
    )
    assert all("oldest" not in parse_qs(urlsplit(url).query) for url, _ in calls)
    assert parse_qs(urlsplit(calls[3][0]).query)["exclude_archived"] == ["false"]
    for url, kwargs in calls:
        assert kwargs["authentication"]["headers"]["Authorization"] == "Bearer " + TOKEN
        assert kwargs["allowed_origin"] == "https://slack.com"
        assert TOKEN not in url
        assert kwargs["max_bytes"] == 2 * 1024 * 1024
    assert connector.reader._authentication is None


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError(TOKEN),
        {"ok": False, "error": "missing_scope"},
        {"ok": False, "error": "ratelimited"},
        {"messages": []},
        {"ok": True, "messages": []},
        page("messages", [message("wrong")]),
        page("messages", [message("1400000000.000001", TOKEN)]),
        page("messages", [message()], "new-cursor", True),
        page("messages", [], "first-cursor", True),
        page("messages", [], "", True),
        b'{"ok":true,"ok":false}',
        b'{"ok":true,"value":1e999}',
    ],
)
def test_failed_later_page_preserves_index_and_checkpoint(
    tmp_path, monkeypatch, failure
):
    connector = reader(tmp_path)
    store = KnowledgeStore(db_path=tmp_path / "knowledge.db")
    engine = SyncEngine(IngestionPipeline(store), state_db=str(tmp_path / "state.db"))
    try:
        responses(
            monkeypatch,
            prefix()
            + [page("messages", [message("1300000000.000001", "Prior evidence")])],
        )
        assert engine.sync(connector, on_complete=lambda: "previous-cursor") == 1
        previous = engine.get_checkpoint("slack")
        responses(
            monkeypatch,
            prefix() + [page("messages", [message()], "first-cursor", True), failure],
        )
        with pytest.raises(ValueError, match="Account sync failed") as error:
            engine.sync(connector)
        assert TOKEN not in str(error.value)
        current = engine.get_checkpoint("slack")
        assert current["cursor"] == "previous-cursor"
        assert current["last_sync"] == previous["last_sync"]
        assert store.count() == 1
    finally:
        engine.close()
        store.close()


@pytest.mark.parametrize(
    "failure",
    [
        {"ok": False, "error": "not_in_channel"},
        page(
            "messages",
            [
                message(
                    "1500000001.000001", "Wrong thread", thread_ts="1400000000.000001"
                )
            ],
        ),
    ],
)
def test_thread_failure_does_not_yield_root(tmp_path, monkeypatch, failure):
    connector = reader(tmp_path)
    responses(
        monkeypatch, prefix() + [page("messages", [message(reply_count=1)]), failure]
    )
    with pytest.raises(ValueError):
        next(connector.sync())


def test_failure_in_second_channel_does_not_yield_first(tmp_path, monkeypatch):
    connector = reader(tmp_path)
    responses(
        monkeypatch,
        prefix([CHANNEL, {"id": "D123", "is_im": True, "user": "U123"}])
        + [page("messages", [message()]), {"ok": False, "error": "missing_scope"}],
    )
    with pytest.raises(ValueError):
        next(connector.sync())


@pytest.mark.parametrize(
    "config,pages",
    [
        ({"max_requests": 3}, prefix() + [page("messages", [])]),
        ({"max_conversations": 1}, prefix([CHANNEL, {"id": "C456"}])),
        (
            {"max_documents": 1},
            prefix() + [page("messages", [message(), message("1500000001.000001")])],
        ),
    ],
)
def test_scan_limits_are_errors_not_truncation(tmp_path, monkeypatch, config, pages):
    connector = reader(tmp_path, config)
    responses(monkeypatch, pages)
    with pytest.raises(ValueError):
        next(connector.sync())


def test_cancelled_scan_preserves_checkpoint(tmp_path, monkeypatch):
    connector = reader(tmp_path)
    store = KnowledgeStore(db_path=tmp_path / "knowledge.db")
    engine = SyncEngine(IngestionPipeline(store), state_db=str(tmp_path / "state.db"))

    class Control:
        cancelled = False

        def check(self):
            if self.cancelled:
                raise SyncCancelled()

        def report(self, **kwargs):
            pass

    control = Control()
    try:
        responses(monkeypatch, prefix() + [page("messages", [message()])])
        engine.sync(connector)
        previous = engine.get_checkpoint("slack")
        connector.bind_sync_control(control)

        def cancel(url):
            control.cancelled = True
            return page("messages", [])

        responses(
            monkeypatch,
            prefix() + [page("messages", [message()], "next", True), cancel],
        )
        with pytest.raises(SyncCancelled):
            engine.sync(connector)
        assert engine.get_checkpoint("slack")["last_sync"] == previous["last_sync"]
        assert store.count() == 1
    finally:
        engine.close()
        store.close()


def test_named_manager_refreshes_old_edits_and_retains_missing_evidence(
    tmp_path, monkeypatch
):
    from openjarvis.connectors.source_manager import SourceManager
    from openjarvis.connectors.source_store import SourceStore

    manager = SourceManager(
        SourceStore(
            str(tmp_path / "sources.db"), legacy_path=str(tmp_path / "legacy.json")
        ),
        knowledge_path=str(tmp_path / "knowledge.db"),
    )
    record = manager.create("slack_account", "Workspace", {})
    save_tokens(str(token_path(tmp_path, record["id"])), {"token": TOKEN})
    responses(
        monkeypatch,
        prefix() + [page("messages", [message(), message("1400000000.000001")])],
    )
    assert manager.sync(record["id"]) == 2
    identity = f"source:{record['id']}:slack:example:C123:1500000000.000001"
    with KnowledgeStore(manager.knowledge_path) as store:
        previous = store.document_fingerprint(identity)
    responses(
        monkeypatch,
        prefix() + [page("messages", [message(text="Updated old evidence")])],
    )
    manager.sync(record["id"])
    with KnowledgeStore(manager.knowledge_path) as store:
        assert store.document_fingerprint(identity) != previous
        assert store.count_document_prefix(f"source:{record['id']}:") == 2


@pytest.mark.parametrize(
    "config",
    [
        {"max_requests": True},
        {"max_documents": 10001},
        {"max_conversations": 0},
        {"timeout_seconds": 9},
        {"url": "https://evil.example/"},
    ],
)
def test_config_validation(config):
    with pytest.raises(ValueError):
        validate_config("slack", config)


def test_empty_configuration_remains_compatible():
    from openjarvis.connectors.source_adapters import get_adapter

    assert validate_config("slack", {}) == {}
    assert {
        f["name"]: f["default_value"] for f in get_adapter("slack_account").fields
    } == {
        "max_conversations": 200,
        "max_requests": 500,
        "max_documents": 5000,
        "timeout_seconds": 120,
    }


@pytest.mark.parametrize("status", [401, 429, 500, 302])
def test_http_errors_do_not_yield_partial_scan(tmp_path, monkeypatch, status):
    connector = reader(tmp_path)

    def failure(url):
        return httpx.Response(status, request=httpx.Request("GET", url))

    calls = responses(monkeypatch, prefix() + [failure])
    with pytest.raises(ValueError):
        next(connector.sync())
    assert len(calls) == 4  # No blocking or unbounded rate-limit retries.


def test_response_redirect_is_rejected(tmp_path, monkeypatch):
    connector = reader(tmp_path)
    responses(
        monkeypatch,
        [
            httpx.Response(
                200,
                json=AUTH,
                request=httpx.Request("GET", "https://other.example/api/auth.test"),
            )
        ],
    )
    with pytest.raises(ValueError):
        next(connector.sync())


def test_empty_thread_is_failure(tmp_path, monkeypatch):
    connector = reader(tmp_path)
    responses(
        monkeypatch,
        prefix() + [page("messages", [message(reply_count=1)]), page("messages", [])],
    )
    with pytest.raises(ValueError):
        next(connector.sync())


def test_deadline_is_enforced_after_response(tmp_path, monkeypatch):
    connector = reader(tmp_path, {"timeout_seconds": 10})
    clock = [0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    def delayed(url):
        clock[0] = 11
        return AUTH

    responses(monkeypatch, [delayed])
    with pytest.raises(ValueError):
        next(connector.sync())


def test_response_size_is_bounded(tmp_path, monkeypatch):
    connector = reader(tmp_path)
    responses(monkeypatch, [b" " * (2 * 1024 * 1024 + 1)])
    with pytest.raises(ValueError):
        next(connector.sync())


def test_credentials_are_bound_to_each_instance(tmp_path, monkeypatch):
    first, second = reader(tmp_path), reader(tmp_path)
    save_tokens(str(second.path), {"token": "xoxp-second-synthetic-value"})
    calls = responses(monkeypatch, prefix([]) + prefix([]))
    assert list(first.sync()) == []
    assert list(second.sync()) == []
    assert calls[0][1]["authentication"]["secret"] == TOKEN
    assert calls[3][1]["authentication"]["secret"] == "xoxp-second-synthetic-value"


@pytest.mark.parametrize("stage", ["auth", "users", "channels"])
def test_early_collection_failures_are_not_empty_success(tmp_path, monkeypatch, stage):
    connector = reader(tmp_path)
    pages = prefix()
    pages[{"auth": 0, "users": 1, "channels": 2}[stage]] = {
        "ok": False,
        "error": "missing_scope",
    }
    calls = responses(monkeypatch, pages)
    with pytest.raises(ValueError):
        next(connector.sync())
    assert len(calls) == {"auth": 1, "users": 2, "channels": 3}[stage]


def test_retention_limit_is_disclosed_and_bot_blocks_are_preserved(
    tmp_path, monkeypatch
):
    connector = reader(tmp_path)
    bot = message(
        "1500000001.000001",
        "",
        bot_id="B123",
        subtype="bot_message",
        blocks=[
            {
                "type": "section",
                "text": {"type": "plain_text", "text": "Evidence from a bot"},
            }
        ],
    )
    bot.pop("user")
    data = page("messages", [bot])
    data["is_limited"] = True
    responses(monkeypatch, prefix() + [data])
    docs = list(connector.sync())
    assert "Evidence from a bot" in docs[0].content
    assert docs[0].metadata["provider_history_limited"] is True
    assert docs[0].author == "B123"


@pytest.mark.parametrize("field", ["members", "channels"])
def test_directory_pagination_must_be_explicit(tmp_path, monkeypatch, field):
    connector = reader(tmp_path)
    pages = prefix()
    pages[1 if field == "members" else 2] = {"ok": True, field: []}
    responses(monkeypatch, pages)
    with pytest.raises(ValueError):
        next(connector.sync())
