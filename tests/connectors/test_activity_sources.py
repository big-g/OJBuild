"""Named activity scans, using synthetic responses only; no provider traffic."""

import json
import socket
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from openjarvis.connectors import activity_sources as module
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


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Live provider traffic forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)


def reader(tmp_path, service="strava", config=None, identity=None):
    import uuid

    identity = identity or str(uuid.uuid4())
    record = {"id": identity, "config": config or {}}
    save_tokens(
        str(token_path(tmp_path, identity)),
        {"access_token": "protected-test-access-value"},
    )
    return AccountSource(service, record, tmp_path)


def activity(identity=1, name="Morning Run"):
    return {
        "id": identity,
        "name": name,
        "sport_type": "Run",
        "start_date": "2020-01-01T10:00:00Z",
        "distance": 5200,
        "moving_time": 1560,
    }


def play(identity="track1", timestamp="2026-09-30T10:00:00Z"):
    return {
        "track": {
            "id": identity,
            "name": "Test track",
            "artists": [{"name": "Test artist"}],
        },
        "played_at": timestamp,
    }


def responses(monkeypatch, pages):
    calls = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        payload = pages[len(calls) - 1]
        if isinstance(payload, Exception):
            raise payload
        if callable(payload):
            payload = payload(url)
        if isinstance(payload, httpx.Response):
            return payload
        return httpx.Response(
            200,
            content=json.dumps(payload).encode()
            if not isinstance(payload, bytes)
            else payload,
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(module, "fetch_public_source", fetch)
    return calls


def test_strava_short_pages_and_old_edits_are_not_skipped(tmp_path, monkeypatch):
    connector = reader(tmp_path)
    calls = responses(monkeypatch, [[activity()], [activity(2)], []])
    docs = list(
        connector.sync(since=datetime(2026, 10, 1, tzinfo=timezone.utc), cursor="old")
    )
    assert len(docs) == 2
    assert [parse_qs(urlsplit(url).query)["page"] for url, _ in calls] == [
        ["1"],
        ["2"],
        ["3"],
    ]
    assert all("after" not in parse_qs(urlsplit(url).query) for url, _ in calls)
    assert docs[0].timestamp.year == 2020
    assert docs[0].metadata["coverage"] == "accessible_activities"
    for url, kwargs in calls:
        assert (
            kwargs["authentication"]["headers"]["Authorization"]
            == "Bearer protected-test-access-value"
        )
        assert kwargs["allowed_origin"] == "https://www.strava.com"
        assert kwargs["deadline"] > 0
        assert "protected-test-access-value" not in url
    assert connector.reader._authentication is None


def test_spotify_paginates_and_filters_overlap(tmp_path, monkeypatch):
    connector = reader(tmp_path, "spotify")
    older = int(datetime(2026, 9, 30, 10, tzinfo=timezone.utc).timestamp() * 1000)
    next_url = (
        "https://api.spotify.com/v1/me/player/recently-played?before="
        + str(older)
        + "&limit=50"
    )
    calls = responses(
        monkeypatch,
        [
            {"items": [play()], "next": next_url},
            {"items": [play("track2", "2026-09-29T10:00:00Z")], "next": None},
        ],
    )
    docs = list(connector.sync(since=datetime(2026, 9, 30, tzinfo=timezone.utc)))
    assert len(calls) == 2
    assert len(docs) == 1
    assert docs[0].doc_id == "spotify-track1-2026-09-30T10:00:00Z"
    assert docs[0].metadata["coverage"] == "provider_available_recent_history"


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("protected-test-access-value"),
        {"error": "unauthorized"},
        [activity(3)],
        [{**activity(2), "start_date": "not-a-date"}],
        [{**activity(2), "start_date": "2020-01-01T10:00:00"}],
        [{**activity(2), "name": "protected-test-access-value"}],
        b'[{"id":2,"id":3}]',
        b'[{"id":2,"distance":1e999}]',
    ],
)
def test_failed_second_page_preserves_checkpoint_and_index(
    tmp_path, monkeypatch, failure
):
    connector = reader(tmp_path)
    store = KnowledgeStore(db_path=tmp_path / "knowledge.db")
    engine = SyncEngine(IngestionPipeline(store), state_db=str(tmp_path / "state.db"))
    try:
        responses(monkeypatch, [[activity()], []])
        assert engine.sync(connector, on_complete=lambda: "prior-cursor") == 1
        prior = engine.get_checkpoint("strava")
        responses(monkeypatch, [[activity(3)], failure])
        with pytest.raises(ValueError, match="Account sync failed") as error:
            engine.sync(connector)
        assert "protected-test-access-value" not in str(error.value)
        current = engine.get_checkpoint("strava")
        assert current["last_sync"] == prior["last_sync"]
        assert current["cursor"] == "prior-cursor"
        assert store.count() == 1
    finally:
        engine.close()
        store.close()


@pytest.mark.parametrize(
    "config,pages",
    [
        ({"max_pages": 1}, [[activity()], []]),
        ({"max_documents": 1}, [[activity(), activity(2)], []]),
    ],
)
def test_limits_do_not_yield_partial_docs(tmp_path, monkeypatch, config, pages):
    connector = reader(tmp_path, config=config)
    responses(monkeypatch, pages)
    iterator = connector.sync()
    with pytest.raises(ValueError):
        next(iterator)


@pytest.mark.parametrize(
    "next_url",
    [
        "https://evil.example/?before=1&limit=50",
        "https://api.spotify.com/v1/me/player/recently-played?before=1&limit=50",
        "https://api.spotify.com/v1/me/player/recently-played?before=1&before=2&limit=50",
        "https://api.spotify.com/v1/me/player/recently-played?after=1&limit=50",
    ],
)
def test_spotify_rejects_unsafe_or_skipping_cursor(tmp_path, monkeypatch, next_url):
    connector = reader(tmp_path, "spotify")
    calls = responses(monkeypatch, [{"items": [play()], "next": next_url}])
    with pytest.raises(ValueError):
        next(connector.sync(since=datetime(2026, 9, 30, tzinfo=timezone.utc)))
    assert len(calls) == 1


def test_cancellation_preserves_checkpoint(tmp_path, monkeypatch):
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
        responses(monkeypatch, [[activity()], []])
        engine.sync(connector)
        prior = engine.get_checkpoint("strava")
        connector.bind_sync_control(control)

        def cancel(url):
            control.cancelled = True
            return []

        responses(monkeypatch, [[activity(2)], cancel])
        with pytest.raises(SyncCancelled):
            engine.sync(connector)
        assert engine.get_checkpoint("strava")["last_sync"] == prior["last_sync"]
        assert store.count() == 1
    finally:
        engine.close()
        store.close()


def test_account_tokens_are_isolated(tmp_path, monkeypatch):
    first, second = reader(tmp_path), reader(tmp_path)
    save_tokens(str(second.path), {"access_token": "second-protected-access"})
    calls = responses(monkeypatch, [[], []])
    list(first.sync())
    list(second.sync())
    assert calls[0][1]["authentication"]["secret"] == "protected-test-access-value"
    assert calls[1][1]["authentication"]["secret"] == "second-protected-access"


@pytest.mark.parametrize(
    "config",
    [
        {"max_pages": True},
        {"max_pages": 251},
        {"max_documents": 0},
        {"timeout_seconds": 9},
        {"url": "https://example.com"},
    ],
)
def test_configuration_bounds(config):
    with pytest.raises(ValueError):
        validate_config("strava", config)


def test_config_defaults_keep_existing_records_compatible():
    from openjarvis.connectors.source_adapters import get_adapter

    assert validate_config("spotify", {}) == {}
    fields = get_adapter("spotify_account").fields
    assert {field["name"]: field["default_value"] for field in fields} == {
        "max_pages": 100,
        "max_documents": 5000,
        "timeout_seconds": 120,
    }


def test_named_manager_refreshes_old_content_without_deletion_inference(
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
    record = manager.create("strava_account", "Running", {})
    save_tokens(
        str(token_path(tmp_path, record["id"])),
        {"access_token": "protected-test-access-value"},
    )
    responses(monkeypatch, [[activity(), activity(2)], []])
    assert manager.sync(record["id"]) == 2
    identity = f"source:{record['id']}:strava-1"
    with KnowledgeStore(manager.knowledge_path) as store:
        before = store.document_fingerprint(identity)
    responses(monkeypatch, [[activity(name="Updated historical run")], []])
    manager.sync(record["id"])
    with KnowledgeStore(manager.knowledge_path) as store:
        assert store.document_fingerprint(identity) != before
        assert store.count_document_prefix(f"source:{record['id']}:") == 2


@pytest.mark.parametrize("status", [401, 429, 500, 302])
def test_provider_http_errors_fail_before_yield(tmp_path, monkeypatch, status):
    connector = reader(tmp_path)

    def error(url):
        return httpx.Response(status, request=httpx.Request("GET", url))

    responses(monkeypatch, [error])
    with pytest.raises(ValueError):
        next(connector.sync())


def test_scan_deadline_is_enforced_after_response(tmp_path, monkeypatch):
    connector = reader(tmp_path, config={"timeout_seconds": 10})
    clock = [0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    def delayed(url):
        clock[0] = 11
        return []

    responses(monkeypatch, [delayed])
    with pytest.raises(ValueError):
        next(connector.sync())


def test_response_byte_limit_is_enforced(tmp_path, monkeypatch):
    connector = reader(tmp_path)
    responses(monkeypatch, [b" " * (2 * 1024 * 1024 + 1)])
    with pytest.raises(ValueError):
        next(connector.sync())


@pytest.mark.parametrize(
    "payload",
    [
        {"items": []},
        {
            "items": [],
            "next": "https://api.spotify.com/v1/me/player/recently-played?before=1&limit=50",
        },
        {"items": [{"track": {}, "played_at": "wrong"}], "next": None},
    ],
)
def test_spotify_malformed_page_is_not_a_success(tmp_path, monkeypatch, payload):
    connector = reader(tmp_path, "spotify")
    responses(monkeypatch, [payload])
    with pytest.raises(ValueError):
        next(connector.sync())


def test_spotify_local_track_has_stable_identity(tmp_path, monkeypatch):
    connector = reader(tmp_path, "spotify")
    item = play()
    item["track"].update(id=None, uri="spotify:local:artist:album:track:100")
    responses(monkeypatch, [{"items": [item], "next": None}])
    docs = list(connector.sync(since=datetime(2026, 9, 30, tzinfo=timezone.utc)))
    assert docs[0].doc_id.startswith("spotify-local-")
