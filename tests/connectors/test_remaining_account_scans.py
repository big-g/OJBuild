"""Remaining named readers: synthetic pages and an outbound-network ban."""

import base64
import json
import socket
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from openjarvis.connectors import account_scans as module
from openjarvis.connectors.account_scans import REMAINING_SERVICES
from openjarvis.connectors.instance_sources import (
    AccountSource,
    token_path,
    validate_config,
)
from openjarvis.connectors.oauth import load_tokens, save_tokens
from openjarvis.connectors.pipeline import IngestionPipeline
from openjarvis.connectors.store import KnowledgeStore
from openjarvis.connectors.sync_control import SyncCancelled
from openjarvis.connectors.sync_engine import SyncEngine

SERVICES = sorted(REMAINING_SERVICES)
TOKEN = "protected-synthetic-access-value"
TIME = "2020-01-01T00:00:00Z"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Provider traffic forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)


def reader(tmp_path, service, config=None):
    config = {
        **({"location": "Boston"} if service == "weather" else {}),
        **(config or {}),
    }
    record = {"id": str(uuid.uuid4()), "config": config}
    field = (
        "api_key"
        if service == "weather"
        else "access_token"
        if service in {"gmail", "gdrive", "gcalendar", "gcontacts", "google_tasks"}
        else "token"
    )
    save_tokens(str(token_path(tmp_path, record["id"])), {field: TOKEN})
    return AccountSource(service, record, tmp_path)


def fetches(monkeypatch, pages):
    calls = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        payload = pages[len(calls) - 1]
        if isinstance(payload, Exception):
            raise payload
        if callable(payload):
            payload = payload(url, kwargs)
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


def gmail_message(identity):
    return {
        "id": identity,
        "internalDate": "1577836800000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": "Subject", "value": "Message " + identity}],
            "body": {"data": base64.urlsafe_b64encode(b"Actual email body").decode()},
        },
    }


def pages(service):
    if service == "gmail":
        return [
            {"messages": [{"id": "one"}], "nextPageToken": "page-two"},
            gmail_message("one"),
            {"messages": [{"id": "two"}]},
            gmail_message("two"),
        ]
    if service == "gdrive":

        def file(identity):
            return {
                "id": identity,
                "name": "Document",
                "mimeType": "application/pdf",
                "modifiedTime": TIME,
            }

        return [
            {
                "files": [file("one")],
                "incompleteSearch": False,
                "nextPageToken": "page-two",
            },
            {"files": [file("two")], "incompleteSearch": False},
        ]
    if service == "gcalendar":
        event = {"id": "same-event", "summary": "Meeting", "updated": TIME}
        return [
            {"items": [{"id": "calendar-one"}], "nextPageToken": "cal-page"},
            {"items": [event]},
            {"items": [{"id": "calendar-two"}]},
            {"items": [event]},
        ]
    if service == "gcontacts":

        def contact(identity):
            return {
                "resourceName": "people/" + identity,
                "names": [{"displayName": "Person"}],
            }

        return [
            {"connections": [contact("one")], "nextPageToken": "page-two"},
            {"connections": [contact("two")]},
        ]
    if service == "google_tasks":
        task = {"id": "same-task", "title": "Task", "updated": TIME}
        return [
            {"items": [{"id": "list-one"}], "nextPageToken": "list-page"},
            {"items": [task]},
            {"items": [{"id": "list-two"}]},
            {"items": [task]},
        ]
    if service == "dropbox":

        def file(identity):
            return {
                ".tag": "file",
                "id": "id:" + identity,
                "rev": "rev-" + identity,
                "path_lower": "/" + identity + ".pdf",
                "name": identity + ".pdf",
                "server_modified": TIME,
            }

        return [
            {"entries": [file("one")], "cursor": "page-two", "has_more": True},
            {"entries": [file("two")], "cursor": "terminal", "has_more": False},
        ]
    if service == "granola":

        def note(identity):
            return {
                "id": identity,
                "title": "Meeting",
                "summary_markdown": "Real summary",
                "updated_at": TIME,
            }

        def turn(text):
            return {
                "text": text,
                "start_time": TIME,
                "end_time": TIME,
                "speaker": {"name": "Alice"},
            }

        return [
            {
                "notes": [{"id": "not_12345678901234"}],
                "hasMore": True,
                "cursor": "note-page",
            },
            note("not_12345678901234"),
            {
                "transcript": [turn("First transcript page")],
                "hasMore": True,
                "cursor": "turn-page",
            },
            {
                "transcript": [turn("Second transcript page")],
                "hasMore": False,
                "cursor": None,
            },
            {"notes": [{"id": "not_abcdefghijklmn"}], "hasMore": False, "cursor": None},
            note("not_abcdefghijklmn"),
            {"transcript": [], "hasMore": False, "cursor": None},
        ]
    if service == "oura":
        day = datetime.now(timezone.utc).date().isoformat()
        return [
            {"data": [{"id": "night", "day": day}], "next_token": "sleep-page"},
            {"data": [{"id": "nap", "day": day}], "next_token": None},
            {"data": [], "next_token": None},
            {"data": [], "next_token": None},
        ]
    if service == "github_notifications":

        def notif(identity):
            return {
                "id": identity,
                "subject": {"title": "Notification"},
                "updated_at": TIME,
            }

        def first(url, kwargs):
            query = parse_qs(urlsplit(url).query)
            next_url = url.replace("page=1", "page=2")
            assert query["all"] == ["true"]
            return httpx.Response(
                200,
                json=[notif("one")],
                headers={"Link": "<" + next_url + '>; rel="next"'},
                request=httpx.Request("GET", url),
            )

        return [first, [notif("two")]]
    if service == "weather":
        now = int(datetime.now(timezone.utc).timestamp())

        def weather(dt):
            return {
                "dt": dt,
                "main": {"temp": 60},
                "weather": [{"description": "Cloudy"}],
            }

        return [
            {**weather(now), "cod": 200},
            {
                "cod": "200",
                "cnt": 4,
                "list": [weather(now + n * 10800) for n in range(1, 5)],
            },
        ]
    raise AssertionError(service)


@pytest.mark.parametrize("service", SERVICES)
def test_complete_provider_scans_have_isolated_stable_ids(
    tmp_path, monkeypatch, service
):
    connector = reader(tmp_path, service)
    calls = fetches(monkeypatch, pages(service))
    docs = list(connector.sync(since=datetime.now(timezone.utc), cursor="obsolete"))
    assert len(docs) == 2
    assert len({doc.source_id for doc in docs}) == 2
    assert all(
        doc.source_id == doc.doc_id and doc.metadata["content_version"] for doc in docs
    )
    assert connector.reader._authentication is None
    for url, kwargs in calls:
        assert TOKEN not in url
        assert kwargs["authentication"]["secret"] == TOKEN
        assert kwargs["allowed_origin"] in connector.reader.origins
        assert kwargs["max_bytes"] == 2 * 1024 * 1024
    if service == "gmail":
        assert docs[0].content == "Actual email body"
    if service == "granola":
        assert "Second transcript page" in docs[0].content
    if service == "oura":
        assert docs[0].doc_id != docs[1].doc_id
    if service == "google_tasks":
        assert parse_qs(urlsplit(calls[1][0]).query)["showHidden"] == ["true"]
    if service == "weather":
        assert calls[0][1]["authentication"]["query"] == {"appid": TOKEN}


@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize(
    "failure", [RuntimeError(TOKEN), {}, b'{"a":1,"a":2}', b'{"value":1e999}']
)
def test_late_failure_preserves_checkpoint_and_prior_index(
    tmp_path, monkeypatch, service, failure
):
    connector = reader(tmp_path, service)
    store = KnowledgeStore(tmp_path / "knowledge.db")
    engine = SyncEngine(IngestionPipeline(store), state_db=str(tmp_path / "state.db"))
    try:
        fetches(monkeypatch, pages(service))
        engine.sync(connector, on_complete=lambda: "prior-cursor")
        prior = engine.get_checkpoint(service)
        count = store.count()
        broken = pages(service)
        broken[-1] = failure
        fetches(monkeypatch, broken)
        with pytest.raises(ValueError, match="Account sync failed") as error:
            engine.sync(connector)
        assert TOKEN not in str(error.value)
        checkpoint = engine.get_checkpoint(service)
        assert checkpoint["last_sync"] == prior["last_sync"]
        assert checkpoint["cursor"] == prior["cursor"]
        assert store.count() == count
    finally:
        engine.close()
        store.close()


@pytest.mark.parametrize("service", SERVICES)
def test_document_limits_fail_without_yielding_partial_scan(
    tmp_path, monkeypatch, service
):
    connector = reader(tmp_path, service, {"max_documents": 1})
    fetches(monkeypatch, pages(service))
    with pytest.raises(ValueError):
        next(connector.sync())


@pytest.mark.parametrize("service", SERVICES)
def test_request_limits_are_not_successful_truncation(tmp_path, monkeypatch, service):
    connector = reader(tmp_path, service, {"max_requests": 1})
    fetches(monkeypatch, pages(service))
    with pytest.raises(ValueError):
        next(connector.sync())


@pytest.mark.parametrize("service", SERVICES)
def test_cancelled_reads_preserve_prior_watermark(tmp_path, monkeypatch, service):
    connector = reader(tmp_path, service)

    class Control:
        cancelled = False

        def check(self):
            if self.cancelled:
                raise SyncCancelled()

        def report(self, **kwargs):
            pass

    control = Control()
    store = KnowledgeStore(tmp_path / "knowledge.db")
    engine = SyncEngine(IngestionPipeline(store), state_db=str(tmp_path / "state.db"))
    try:
        fetches(monkeypatch, pages(service))
        engine.sync(connector)
        prior = engine.get_checkpoint(service)
        connector.bind_sync_control(control)
        broken = pages(service)

        def cancel(url, kwargs):
            control.cancelled = True
            return {}

        broken[-1] = cancel
        fetches(monkeypatch, broken)
        with pytest.raises(SyncCancelled):
            engine.sync(connector)
        assert engine.get_checkpoint(service)["last_sync"] == prior["last_sync"]
    finally:
        engine.close()
        store.close()


@pytest.mark.parametrize("service", SERVICES)
def test_existing_adapter_upgrade_resets_index_but_retains_credentials(
    tmp_path, monkeypatch, service
):
    from openjarvis.connectors.source_manager import SourceManager
    from openjarvis.connectors.source_store import SourceStore

    manager = SourceManager(
        SourceStore(
            str(tmp_path / "sources.db"), legacy_path=str(tmp_path / "legacy.json")
        ),
        knowledge_path=str(tmp_path / "knowledge.db"),
    )
    config = {"location": "Boston"} if service == "weather" else {}
    record = manager.store.create(service + "_account", "Old connection", config, 1)
    field = (
        "api_key"
        if service == "weather"
        else "access_token"
        if service.startswith("g")
        and service not in {"granola", "github_notifications"}
        else "token"
    )
    save_tokens(str(token_path(tmp_path, record["id"])), {field: TOKEN})
    with KnowledgeStore(manager.knowledge_path) as knowledge:
        knowledge.store(
            "Old conflated evidence",
            source=service,
            doc_id=f"source:{record['id']}:old-record",
        )
    with pytest.raises(ValueError, match="migration"):
        manager.sync(record["id"])
    preview = manager.migration_preview(record["id"], 1)
    assert preview["index_reset"] is True
    migrated = manager.migrate(record["id"], 1, preview["plan_token"])
    assert migrated["config_version"] == 2
    with KnowledgeStore(manager.knowledge_path) as knowledge:
        assert knowledge.count_document_prefix(f"source:{record['id']}:") == 0
    assert load_tokens(str(token_path(tmp_path, record["id"])))[field] == TOKEN
    fetches(monkeypatch, pages(service))
    assert manager.sync(record["id"]) >= 2


@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize(
    "config",
    [
        {"max_requests": True},
        {"max_documents": 0},
        {"timeout_seconds": 9},
        {"max_requests": 2001},
    ],
)
def test_config_limits(service, config):
    with pytest.raises(ValueError):
        validate_config(
            service,
            {**({"location": "Boston"} if service == "weather" else {}), **config},
        )


@pytest.mark.parametrize("service", SERVICES)
def test_reflected_credentials_fail_the_whole_scan(tmp_path, monkeypatch, service):
    connector = reader(tmp_path, service)
    broken = pages(service)
    broken[-1] = {"error": TOKEN}
    fetches(monkeypatch, broken)
    with pytest.raises(ValueError) as error:
        next(connector.sync())
    assert TOKEN not in str(error.value)


def test_drive_export_success(tmp_path, monkeypatch):
    connector = reader(tmp_path, "gdrive")
    calls = fetches(
        monkeypatch,
        [
            {
                "files": [
                    {
                        "id": "one",
                        "name": "Doc",
                        "mimeType": "application/vnd.google-apps.document",
                        "modifiedTime": TIME,
                    }
                ],
                "incompleteSearch": False,
            },
            b"Exported document text",
        ],
    )
    documents = list(connector.sync())
    assert len(documents) == 1
    assert documents[0].content == "Exported document text"
    assert documents[0].metadata["content_kind"] == "exported_text"
    assert "/files/one/export" in calls[1][0]


def test_dropbox_text_download_success(tmp_path, monkeypatch):
    connector = reader(tmp_path, "dropbox")
    item = {
        ".tag": "file",
        "id": "id:one",
        "rev": "v1",
        "path_lower": "/note.txt",
        "name": "note.txt",
        "server_modified": TIME,
    }

    def downloaded(url, kwargs):
        assert kwargs["method"] == "POST" and kwargs["body"] == b""
        assert (
            json.loads(kwargs["authentication"]["headers"]["Dropbox-API-Arg"])["path"]
            == "rev:v1"
        )
        return httpx.Response(
            200,
            content=b"Downloaded text",
            headers={"Dropbox-API-Result": json.dumps({"id": "id:one", "rev": "v1"})},
            request=httpx.Request("POST", url),
        )

    fetches(
        monkeypatch,
        [{"entries": [item], "has_more": False, "cursor": "done"}, downloaded],
    )
    documents = list(connector.sync())
    assert len(documents) == 1
    assert documents[0].content == "Downloaded text"


def test_drive_export_failure_cannot_replace_text_with_metadata(tmp_path, monkeypatch):
    connector = reader(tmp_path, "gdrive")
    first = {
        "files": [
            {
                "id": "one",
                "name": "Doc",
                "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": TIME,
            }
        ],
        "incompleteSearch": False,
    }
    calls = fetches(monkeypatch, [first, RuntimeError("Export failed")])
    with pytest.raises(ValueError):
        next(connector.sync())
    assert len(calls) == 2


@pytest.mark.parametrize("failure", [RuntimeError("Download failed"), "wrong-revision"])
def test_dropbox_download_failures_are_not_metadata_fallback(
    tmp_path, monkeypatch, failure
):
    connector = reader(tmp_path, "dropbox")
    item = {
        ".tag": "file",
        "id": "id:one",
        "rev": "v1",
        "path_lower": "/note.txt",
        "name": "note.txt",
        "server_modified": TIME,
    }

    def downloaded(url, kwargs):
        assert kwargs["method"] == "POST" and kwargs["body"] == b""
        assert (
            json.loads(kwargs["authentication"]["headers"]["Dropbox-API-Arg"])["path"]
            == "rev:v1"
        )
        return httpx.Response(
            200,
            content=b"Actual text",
            headers={
                "Dropbox-API-Result": json.dumps({"id": "id:one", "rev": failure})
            },
            request=httpx.Request("POST", url),
        )

    fetches(
        monkeypatch,
        [
            {"entries": [item], "has_more": False, "cursor": "done"},
            failure if isinstance(failure, Exception) else downloaded,
        ],
    )
    with pytest.raises(ValueError):
        next(connector.sync())


@pytest.mark.parametrize("service", ["gcontacts", "gdrive", "dropbox", "oura"])
def test_repeated_page_record_fails(tmp_path, monkeypatch, service):
    connector = reader(tmp_path, service)
    broken = pages(service)
    field = {
        "gcontacts": "connections",
        "gdrive": "files",
        "dropbox": "entries",
        "oura": "data",
    }[service]
    broken[1][field] = broken[0][field]
    fetches(monkeypatch, broken)
    with pytest.raises(ValueError):
        next(connector.sync())


@pytest.mark.parametrize("service", ["gcontacts", "gdrive", "dropbox", "oura"])
def test_repeated_page_cursor_fails(tmp_path, monkeypatch, service):
    connector = reader(tmp_path, service)
    broken = pages(service)
    field = (
        "cursor"
        if service == "dropbox"
        else "next_token"
        if service == "oura"
        else "nextPageToken"
    )
    broken[1][field] = broken[0][field]
    if service == "dropbox":
        broken[1]["has_more"] = True
    fetches(monkeypatch, broken)
    with pytest.raises(ValueError):
        next(connector.sync())


def test_drive_incomplete_search_is_not_a_success(tmp_path, monkeypatch):
    connector = reader(tmp_path, "gdrive")
    broken = pages("gdrive")
    broken[0]["incompleteSearch"] = True
    fetches(monkeypatch, broken)
    with pytest.raises(ValueError):
        next(connector.sync())


def test_oura_missing_pagination_flag_is_not_a_success(tmp_path, monkeypatch):
    connector = reader(tmp_path, "oura")
    fetches(monkeypatch, [{"data": []}])
    with pytest.raises(ValueError):
        next(connector.sync())


def test_github_foreign_next_url_is_never_fetched(tmp_path, monkeypatch):
    connector = reader(tmp_path, "github_notifications")

    def first(url, kwargs):
        return httpx.Response(
            200,
            json=[],
            headers={"Link": '<https://attacker.example/?page=2>; rel="next"'},
            request=httpx.Request("GET", url),
        )

    calls = fetches(monkeypatch, [first])
    with pytest.raises(ValueError):
        next(connector.sync())
    assert len(calls) == 1


@pytest.mark.parametrize("service", SERVICES)
def test_per_instance_credential_binding(tmp_path, monkeypatch, service):
    first, second = reader(tmp_path, service), reader(tmp_path, service)
    bundle = load_tokens(str(second.path))
    field = next(iter(bundle))
    bundle[field] = "second-synthetic-secret"
    save_tokens(str(second.path), bundle)
    calls = fetches(monkeypatch, pages(service))
    list(first.sync())
    assert calls[0][1]["authentication"]["secret"] == TOKEN
    calls = fetches(monkeypatch, pages(service))
    list(second.sync())
    assert calls[0][1]["authentication"]["secret"] == "second-synthetic-secret"


@pytest.mark.parametrize("service", SERVICES)
def test_deadlines_and_byte_caps_are_enforced(tmp_path, monkeypatch, service):
    connector = reader(tmp_path, service, {"timeout_seconds": 10})
    clock = [0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    def delayed(url, kwargs):
        clock[0] = 11
        return {}

    fetches(monkeypatch, [delayed])
    with pytest.raises(ValueError):
        next(connector.sync())
    clock[0] = 0
    fetches(monkeypatch, [b" " * (2 * 1024 * 1024 + 1)])
    with pytest.raises(ValueError):
        next(connector.sync())
