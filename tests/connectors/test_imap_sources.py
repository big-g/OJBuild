"""Synthetic named IMAP contracts; no provider traffic."""

import json
import socket
from unittest.mock import Mock

import pytest

from openjarvis.connectors import imap_sources as module
from openjarvis.connectors.instance_sources import token_path
from openjarvis.connectors.oauth import save_tokens
from openjarvis.connectors.sync_control import SyncCancelled, SyncLimitExceeded
from tests.server import test_source_connections as connection_tests

account_setup = connection_tests.setup

PASSWORD = "synthetic protected password"
RAW = (
    b"Subject: Evidence\r\nFrom: sender@example.com\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n\r\nActual evidence"
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    for name in ("getaddrinfo", "create_connection"):
        monkeypatch.setattr(
            socket, name, Mock(side_effect=AssertionError("No network"))
        )


class Mailbox:
    def __init__(self, failure=None):
        self.failure, self.closed, self.selects = failure, False, 0
        self.validity = b"123"

    def login(self, username, password):
        assert username == "mail@example.com" and password == PASSWORD
        if self.failure == "login":
            raise ValueError(PASSWORD)
        return "OK", []

    def select(self, mailbox, readonly):
        assert mailbox == '"INBOX"' and readonly is True
        self.selects += 1
        if self.failure == "epoch" and self.selects > 1:
            self.validity = b"456"
        return "OK", [b"2"]

    def response(self, code):
        return code, [self.validity]

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b"1 1" if self.failure == "duplicate" else b"1 2"]
        uid, fields = args
        assert fields == "(UID INTERNALDATE RFC822.SIZE BODY.PEEK[])"
        if uid == b"2":
            if self.failure == "late":
                return "NO", [PASSWORD.encode()]
            if self.failure == "missing":
                return "OK", []
        raw = RAW if self.failure != "reflection" else RAW + PASSWORD.encode()
        actual_uid = b"99" if self.failure == "identity" else uid
        header = (
            b"1 (UID "
            + actual_uid
            + b' INTERNALDATE "01-Jan-2020 00:00:00 +0000" RFC822.SIZE '
            + str(len(raw)).encode()
            + b" BODY[] {"
            + str(len(raw)).encode()
            + b"}"
        )
        return "OK", [(header, raw), b")"]

    def shutdown(self):
        self.closed = True


def reader(tmp_path, monkeypatch, failure=None, config=None):
    path = tmp_path / "connectors" / "instance-test.json"
    save_tokens(str(path), {"username": "mail@example.com", "password": PASSWORD})
    source = module.IMAPSource(
        config={"host": "mail.example.com", **(config or {})}, token_path=path
    )
    client = Mailbox(failure)
    monkeypatch.setattr(source, "connect", lambda: client)
    return source, client


def test_complete_readonly_scan(tmp_path, monkeypatch):
    source, client = reader(tmp_path, monkeypatch)
    docs = list(source.sync(since=object(), cursor=None))
    assert len(docs) == 2 and client.closed
    assert docs[0].content == "Actual evidence"
    assert docs[0].doc_id.endswith(":123:1")
    assert docs[0].source_id == docs[0].doc_id
    assert docs[0].timestamp.year == 2020
    first = docs[0].doc_id
    checkpoint = source.sync_status().cursor
    client.validity = b"456"
    with pytest.raises(module.MailboxIdentityChanged):
        list(source.sync(cursor=checkpoint))
    assert list(source.sync())[0].doc_id != first


@pytest.mark.parametrize(
    "failure",
    ["login", "late", "missing", "identity", "duplicate", "reflection", "epoch"],
)
def test_no_partial_yield_or_secret_errors(tmp_path, monkeypatch, failure):
    source, client = reader(tmp_path, monkeypatch, failure)
    with pytest.raises(ValueError) as error:
        next(source.sync())
    assert PASSWORD not in str(error.value)
    assert source.sync_status().state == "error" and client.closed


def test_document_limit_and_cancellation(tmp_path, monkeypatch):
    source, client = reader(tmp_path, monkeypatch, config={"max_messages": 1})
    with pytest.raises(SyncLimitExceeded):
        next(source.sync())
    assert client.closed
    source.bind_sync_control(Mock(check=Mock(side_effect=SyncCancelled())))
    with pytest.raises(SyncCancelled):
        next(source.sync())


@pytest.mark.parametrize(
    "config",
    [
        {"security": "plain"},
        {"host": "https://example.com"},
        {"port": True},
        {"password": "secret"},
        {"mailbox": "INBOX\r\nLOGIN"},
        {"max_messages": 0},
        {"timeout_seconds": 301},
    ],
)
def test_invalid_config(config):
    with pytest.raises(ValueError):
        module.validate_imap_config({"host": "mail.example.com", **config})


def test_literal_limit_before_allocation():
    transport = object.__new__(module._TLS)
    transport.scan = Mock(bytes=0, deadline=module.time.monotonic() + 100)
    transport.sock = Mock()
    with pytest.raises(SyncLimitExceeded):
        transport.read(module.MESSAGE_BYTES + 1)


def test_named_password_lifecycle(account_setup):
    client, manager, directory = account_setup
    response = client.post(
        "/v1/sources",
        json={
            "adapter_id": "imap_account",
            "name": "Mail",
            "config": {"host": "mail.example.com"},
        },
    )
    assert response.status_code == 201, response.text
    record = response.json()
    path = "/v1/sources/" + record["id"]
    body = {"revision": 1, "username": "mail@example.com", "password": PASSWORD}
    assert (
        client.put(
            path + "/connection/password", json=body, headers={"Authorization": ""}
        ).status_code
        == 401
    )
    assert client.put(path + "/connection/password", json=body).status_code == 200
    assert client.put(path + "/connection/password", json=body).status_code == 409
    status = client.get(path + "/connection").json()
    assert status["auth_type"] == "password" and status["connected"]
    assert PASSWORD not in json.dumps(client.get("/v1/sources").json())
    marker = token_path(directory, record["id"])
    assert PASSWORD not in marker.read_text()
    assert PASSWORD.encode() not in (directory / "source_credentials.db").read_bytes()
    assert (
        client.post(path + "/connection/disconnect", json={"revision": 2}).status_code
        == 200
    )
    assert client.get(path + "/connection").json()["connected"] is False
    assert not marker.exists()


@pytest.mark.parametrize("security", ["tls", "starttls"])
def test_pinned_verified_transport(tmp_path, monkeypatch, security):
    source, _ = reader(tmp_path, monkeypatch, config={"security": security})
    monkeypatch.delattr(source, "connect")
    source.deadline = module.time.monotonic() + 120
    source.bytes = 0
    client = Mock()
    client.starttls.return_value = ("OK", [])
    constructor = Mock(return_value=client)
    monkeypatch.setattr(
        module,
        "validate_imap_endpoint",
        lambda *args: ("mail.example.com", ("93.184.216.34",)),
    )
    monkeypatch.setattr(
        module, "_TLS" if security == "tls" else "_StartTLS", constructor
    )
    assert source.connect() is client
    assert constructor.call_args.args == (
        "mail.example.com",
        993 if security == "tls" else 143,
        "93.184.216.34",
    )
    context = (
        constructor.call_args.kwargs.get("ssl_context")
        if security == "tls"
        else client.starttls.call_args.kwargs["ssl_context"]
    )
    assert context.check_hostname and context.verify_mode == module.ssl.CERT_REQUIRED
    assert client.debug == 0


def test_private_destination_never_connects(tmp_path, monkeypatch):
    source, _ = reader(tmp_path, monkeypatch)
    monkeypatch.delattr(source, "connect")
    source.deadline = module.time.monotonic() + 120
    monkeypatch.setattr(
        module,
        "validate_imap_endpoint",
        Mock(side_effect=ValueError("Private destination")),
    )
    constructor = Mock(side_effect=AssertionError("No connection"))
    monkeypatch.setattr(module, "_TLS", constructor)
    with pytest.raises(ValueError):
        source.connect()
    constructor.assert_not_called()


def test_failed_upgrade_closes_without_login(tmp_path, monkeypatch):
    source, _ = reader(tmp_path, monkeypatch, config={"security": "starttls"})
    monkeypatch.delattr(source, "connect")
    source.deadline = module.time.monotonic() + 120
    monkeypatch.setattr(
        module,
        "validate_imap_endpoint",
        lambda *args: ("mail.example.com", ("93.184.216.34",)),
    )
    client = Mock(starttls=Mock(return_value=("NO", [])))
    monkeypatch.setattr(module, "_StartTLS", Mock(return_value=client))
    with pytest.raises(ValueError):
        source.connect()
    client.shutdown.assert_called_once()
    client.login.assert_not_called()


def test_deadline(tmp_path, monkeypatch):
    source, _ = reader(tmp_path, monkeypatch)
    source.deadline = module.time.monotonic() - 1
    with pytest.raises(SyncLimitExceeded, match="time limit"):
        source.check()


def test_failed_named_scan_preserves_checkpoint_and_evidence(
    account_setup, monkeypatch
):
    client, manager, directory = account_setup
    record = client.post(
        "/v1/sources",
        json={
            "adapter_id": "imap_account",
            "name": "Mail",
            "config": {"host": "mail.example.com"},
        },
    ).json()
    save_tokens(
        str(token_path(directory, record["id"])),
        {"username": "mail@example.com", "password": PASSWORD},
    )
    mailbox = Mailbox()
    monkeypatch.setattr(module.IMAPSource, "connect", lambda self: mailbox)
    assert manager.sync(record["id"]) == 2
    from openjarvis.connectors.pipeline import IngestionPipeline
    from openjarvis.connectors.store import KnowledgeStore
    from openjarvis.connectors.sync_engine import SyncEngine

    with KnowledgeStore(manager.knowledge_path) as store:
        with SyncEngine(
            IngestionPipeline(store), state_db=str(manager.store.path)
        ) as engine:
            prior = engine.get_checkpoint(record["id"])
            count = store.count()
            mailbox.failure = "late"
            with pytest.raises(ValueError):
                manager.sync(record["id"])
            current = engine.get_checkpoint(record["id"])
            assert current["last_sync"] == prior["last_sync"]
            assert current["cursor"] == prior["cursor"]
            assert store.count() == count


@pytest.mark.parametrize(
    "payload",
    [
        {"password": PASSWORD},
        {"revision": 1, "username": "mail@example.com", "password": "bad\r\nvalue"},
        {"revision": 1, "username": "mail@example.com", "password": "nonascii\u2603"},
    ],
)
def test_invalid_password_requests_do_not_echo_inputs(account_setup, payload):
    client, _, _ = account_setup
    record = client.post(
        "/v1/sources",
        json={
            "adapter_id": "imap_account",
            "name": "Mail",
            "config": {"host": "mail.example.com"},
        },
    ).json()
    response = client.put(
        "/v1/sources/" + record["id"] + "/connection/password", json=payload
    )
    assert response.status_code in {400, 422}
    assert payload["password"] not in response.text


def test_instances_have_independent_passwords(account_setup):
    from openjarvis.connectors.oauth import load_tokens

    client, _, directory = account_setup
    records = [
        client.post(
            "/v1/sources",
            json={
                "adapter_id": "imap_account",
                "name": name,
                "config": {"host": "mail.example.com"},
            },
        ).json()
        for name in ("Work", "Personal")
    ]
    for i, record in enumerate(records):
        result = client.put(
            "/v1/sources/" + record["id"] + "/connection/password",
            json={
                "revision": 1,
                "username": "mail@example.com",
                "password": PASSWORD + str(i),
            },
        )
        assert result.status_code == 200
    assert (
        load_tokens(str(token_path(directory, records[0]["id"])))["password"]
        == PASSWORD + "0"
    )
    assert (
        client.delete("/v1/sources/" + records[0]["id"] + "?revision=2").status_code
        == 204
    )
    assert (
        load_tokens(str(token_path(directory, records[1]["id"])))["password"]
        == PASSWORD + "1"
    )


def test_attached_messages_are_excluded(tmp_path, monkeypatch):
    source, _ = reader(tmp_path, monkeypatch)
    raw = (
        b"Subject: Mixed\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="x"\r\n\r\n'
        b"--x\r\nContent-Type: text/plain\r\n\r\nInline evidence\r\n"
        b"--x\r\nContent-Type: message/rfc822\r\n"
        b"Content-Disposition: attachment\r\n\r\n"
        b"Subject: Attached\r\nContent-Type: text/plain\r\n\r\n"
        b"Attachment secret\r\n--x--\r\n"
    )
    monkeypatch.setattr(__import__(__name__, fromlist=["RAW"]), "RAW", raw)
    docs = list(source.sync())
    assert "Inline evidence" in docs[0].content
    assert "Attachment secret" not in docs[0].content
