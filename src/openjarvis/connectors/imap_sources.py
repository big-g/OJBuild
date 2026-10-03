"""Named, bounded, read-only IMAP scans; passwords live only in the vault."""

import hashlib
import re
import time
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser

from openjarvis.connectors._stubs import BaseConnector, Document, SyncStatus
from openjarvis.connectors.imap import (
    _normalize_hostname,
    _PinnedIMAP4,
    _PinnedIMAP4SSL,
    validate_imap_endpoint,
)
from openjarvis.connectors.imap_network import (
    tls_context,
    validate_lan_endpoint,
    validate_network_config,
)
from openjarvis.connectors.oauth import load_tokens
from openjarvis.connectors.sync_control import SyncCancelled, SyncLimitExceeded
from openjarvis.connectors.token_vault import TokenVault

MESSAGE_BYTES = 2 * 1024 * 1024
SCAN_BYTES = 16 * 1024 * 1024
LIMITS = {"max_messages": (1000, 10000), "timeout_seconds": (120, 300)}


class MailboxIdentityChanged(ValueError):
    def __init__(self):
        super().__init__(
            "Mailbox identity changed; replace authorization to reset and resync"
        )


def validate_imap_config(config):
    allowed = {
        "host",
        "port",
        "security",
        "mailbox",
        *LIMITS,
        "network_access",
        "lan_addresses",
        "tls_trust",
        "ca_certificate",
    }
    if set(config) - allowed:
        raise ValueError("Unsupported IMAP configuration")
    host = config.get("host")
    if not isinstance(host, str):
        raise ValueError("An IMAP host is required")
    host = _normalize_hostname(host)
    security = config.get("security", "tls")
    if security not in {"tls", "starttls"}:
        raise ValueError("IMAP requires TLS or STARTTLS")
    port = config.get("port", 993 if security == "tls" else 143)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid IMAP port")
    mailbox = config.get("mailbox", "INBOX")
    if (
        not isinstance(mailbox, str)
        or not 1 <= len(mailbox) <= 200
        or any(ord(c) < 32 or ord(c) > 126 for c in mailbox)
    ):
        raise ValueError("Use a printable ASCII IMAP mailbox name")
    result = {
        "host": host,
        "port": port,
        "security": security,
        "mailbox": mailbox,
        **validate_network_config(config),
    }
    for key, (default, maximum) in LIMITS.items():
        value = config.get(key, default)
        if (
            type(value) is not int
            or not (10 if key == "timeout_seconds" else 1) <= value <= maximum
        ):
            raise ValueError("Invalid IMAP scan limit")
        result[key] = value
    return result


class _BoundedTransport:
    """Guard literal allocation and cumulative bytes inside imaplib's parser."""

    def __init__(self, *args, scan, **kwargs):
        self.scan = scan
        super().__init__(*args, **kwargs)

    def _before_read(self):
        self.scan.check()
        self.sock.settimeout(min(10, max(0.01, self.scan.deadline - time.monotonic())))

    def read(self, size):
        self._before_read()
        if size > MESSAGE_BYTES or self.scan.bytes + size > SCAN_BYTES:
            raise SyncLimitExceeded("bytes")
        result = super().read(size)
        self.scan.bytes += len(result)
        return result

    def readline(self):
        self._before_read()
        result = super().readline()
        self.scan.bytes += len(result)
        if self.scan.bytes > SCAN_BYTES:
            raise SyncLimitExceeded("bytes")
        return result


class _TLS(_BoundedTransport, _PinnedIMAP4SSL):
    pass


class _StartTLS(_BoundedTransport, _PinnedIMAP4):
    pass


class IMAPSource(BaseConnector):
    connector_id = "imap"
    display_name = "IMAP mailbox"

    def __init__(self, *, config, token_path):
        self.config = validate_imap_config(config)
        self._token_path = token_path
        self._status = SyncStatus()

    def is_connected(self):
        values = load_tokens(str(self._token_path)) or {}
        return bool(values.get("username") and values.get("password"))

    def disconnect(self):
        TokenVault(self._token_path).delete()

    def sync_status(self):
        return self._status

    def check(self):
        self.check_sync_cancelled()
        if time.monotonic() >= self.deadline:
            raise SyncLimitExceeded("deadline")

    def connect(self):
        if self.config["network_access"] == "lan":
            host, addresses = validate_lan_endpoint(
                self.config["host"], self.config["port"], self.config["lan_addresses"]
            )
        else:
            host, addresses = validate_imap_endpoint(
                self.config["host"], self.config["port"]
            )
        self.check()
        context = tls_context(self.config)
        # Fail closed on transport/TLS failure. A later retry can resolve again.
        cls = _TLS if self.config["security"] == "tls" else _StartTLS
        client = cls(
            host,
            self.config["port"],
            addresses[0],
            scan=self,
            timeout=min(10, self.config["timeout_seconds"]),
            **({"ssl_context": context} if cls is _TLS else {}),
        )
        try:
            client.debug = 0
            if cls is _StartTLS:
                if client.starttls(ssl_context=context)[0] != "OK":
                    raise ValueError("TLS upgrade rejected")
            return client
        except Exception:
            client.shutdown()
            raise

    def call(self, operation, *args, **kwargs):
        self.check()
        status, data = operation(*args, **kwargs)
        self.check()
        if status != "OK":
            raise ValueError("IMAP command rejected")
        return data

    def sync(self, *, since=None, cursor=None):
        values = load_tokens(str(self._token_path)) or {}
        client = None
        self.deadline = time.monotonic() + self.config["timeout_seconds"]
        self.bytes = 0
        self._status = SyncStatus(state="syncing")
        try:
            if not values.get("username") or not values.get("password"):
                raise ValueError("Authorize this source")
            if any(
                values["password"] in value
                for value in self.config.values()
                if isinstance(value, str)
            ):
                raise ValueError("Configuration contains credential material")
            client = self.connect()
            self.call(client.login, values["username"], values["password"])
            mailbox = (
                '"'
                + self.config["mailbox"].replace("\\", "\\\\").replace('"', '\\"')
                + '"'
            )
            self.call(client.select, mailbox, readonly=True)
            code, validity = client.response("UIDVALIDITY")
            if (
                code != "UIDVALIDITY"
                or len(validity) != 1
                or not isinstance(validity[0], bytes)
                or not re.fullmatch(rb"[1-9][0-9]{0,9}", validity[0])
            ):
                raise ValueError("Missing mailbox identity")
            validity = validity[0].decode()
            scope = hashlib.sha256(self.config["mailbox"].encode()).hexdigest()[:24]
            checkpoint = f"imap:{scope}:{validity}"
            if cursor is not None and cursor != checkpoint:
                raise MailboxIdentityChanged()
            inventory = self.call(client.uid, "SEARCH", None, "ALL")
            if len(inventory) != 1 or not isinstance(inventory[0], bytes):
                raise ValueError("Incomplete mailbox inventory")
            ids = inventory[0].split()
            if len(ids) > self.config["max_messages"]:
                raise SyncLimitExceeded("document")
            if len(ids) != len(set(ids)) or any(
                not re.fullmatch(rb"[1-9][0-9]{0,9}", uid) for uid in ids
            ):
                raise ValueError("Invalid message identities")
            documents = []
            for uid in ids:
                parts = self.call(
                    client.uid,
                    "FETCH",
                    uid,
                    "(UID INTERNALDATE RFC822.SIZE BODY.PEEK[])",
                )
                literals = [part for part in parts if isinstance(part, tuple)]
                if len(literals) != 1 or len(literals[0]) != 2:
                    raise ValueError("Missing message body")
                header, raw = literals[0]
                if (
                    not isinstance(header, bytes)
                    or not isinstance(raw, bytes)
                    or len(raw) > MESSAGE_BYTES
                ):
                    raise ValueError("Invalid message response")
                match = re.search(rb"\bUID ([0-9]+)\b", header)
                size = re.search(rb"\bRFC822.SIZE ([0-9]+)\b", header)
                date = re.search(rb'\bINTERNALDATE "([^"]+)"', header)
                if (
                    not match
                    or match[1] != uid
                    or not size
                    or int(size[1]) != len(raw)
                    or not date
                ):
                    raise ValueError("Message identity or size mismatch")
                timestamp = datetime.strptime(
                    date[1].decode(), "%d-%b-%Y %H:%M:%S %z"
                ).astimezone(timezone.utc)
                message = BytesParser(policy=policy.default).parsebytes(raw)
                if message.defects:
                    raise ValueError("Malformed message")
                texts = []
                pending, count = [(message, 0)], 0
                while pending:
                    part, depth = pending.pop()
                    count += 1
                    if count > 200 or depth > 20 or part.defects:
                        raise ValueError("Malformed or oversized MIME structure")
                    if (
                        part.get_content_disposition() == "attachment"
                        or part.get_filename()
                    ):
                        continue
                    if part.is_multipart():
                        pending.extend(
                            (child, depth + 1) for child in reversed(part.get_payload())
                        )
                        continue
                    if part.get_content_type() in {"text/plain", "text/html"}:
                        text = part.get_content()
                        if not isinstance(text, str):
                            raise ValueError("Invalid message text")
                        if part.get_content_type() == "text/html":
                            from bs4 import BeautifulSoup

                            text = BeautifulSoup(text, "html.parser").get_text(
                                " ", strip=True
                            )
                        texts.append(text)
                content = "\n\n".join(texts)
                title, author = (
                    str(message.get("Subject", "")),
                    str(message.get("From", "")),
                )
                if not content.strip():
                    content = f"Subject: {title}\nFrom: {author}\n[No inline text body]"
                if any(
                    values["password"] in value for value in (content, title, author)
                ):
                    raise ValueError("Credential reflection")
                identity = f"imap:{scope}:{validity}:{uid.decode()}"
                documents.append(
                    Document(
                        doc_id=identity,
                        source_id=identity,
                        source="imap",
                        doc_type="email",
                        title=title,
                        author=author,
                        content=content,
                        timestamp=timestamp,
                        metadata={
                            "mailbox": self.config["mailbox"],
                            "uidvalidity": validity,
                            "uid": uid.decode(),
                            "coverage": "configured_mailbox_text_without_attachments",
                            "timestamp_semantics": "internaldate",
                            "fetched_at": datetime.now(timezone.utc).isoformat(),
                            "trust": "auto",
                            "origin": "external",
                        },
                    )
                )
            self.call(client.select, mailbox, readonly=True)
            code, final_validity = client.response("UIDVALIDITY")
            if code != "UIDVALIDITY" or final_validity != [validity.encode()]:
                raise MailboxIdentityChanged()
            self.check()
            self._status = SyncStatus(
                state="idle", items_synced=len(documents), cursor=checkpoint
            )
        except SyncCancelled:
            self._status = SyncStatus(state="cancelled", error="Sync cancelled")
            raise
        except (SyncLimitExceeded, MailboxIdentityChanged) as exc:
            self._status = SyncStatus(state="error", error=str(exc))
            raise
        except Exception:
            self._status = SyncStatus(
                state="error",
                error="IMAP scan failed; check configuration and authorization",
            )
            raise ValueError(self._status.error) from None
        finally:
            values.clear()
            if client is not None:
                # Close the socket without CLOSE/EXPUNGE or a blocking logout.
                try:
                    client.shutdown()
                except Exception:
                    pass
        yield from documents


def register_imap_adapter(register, adapter_type):
    from openjarvis.connectors.instance_sources import token_path
    from openjarvis.connectors.source_adapters import ConfigMigration

    register(
        adapter_type(
            adapter_id="imap_account",
            display_name="IMAP mailbox",
            description=(
                "Read one configured mailbox using TLS and an encrypted "
                "password or app password, with explicit public or LAN "
                "destination policy."
            ),
            config_version=2,
            migrations=(
                ConfigMigration(1, lambda config: config, preserves_index=True),
            ),
            fields=(
                {
                    "name": "network_access",
                    "label": "Destination access",
                    "type": "select",
                    "default_value": "public",
                    "options": [
                        {"value": "public", "label": "Public mail server"},
                        {"value": "lan", "label": "Authorize private LAN mail server"},
                    ],
                    "value_updates": {"public": {"lan_addresses": ""}},
                    "description": (
                        "LAN access applies only to this source's host, port "
                        "and exact authorized IPs."
                    ),
                },
                {
                    "name": "lan_addresses",
                    "label": "Authorized LAN IP addresses",
                    "type": "text",
                    "default_value": "",
                    "required": True,
                    "visible_when": {"field": "network_access", "equals": "lan"},
                    "placeholder": "192.168.1.20, fd00::20",
                    "description": (
                        "Every DNS result must match. Use exact RFC1918 or IPv6 "
                        "unique-local IPs; no ranges, localhost or "
                        "link-local addresses."
                    ),
                },
                {
                    "name": "tls_trust",
                    "label": "Certificate trust",
                    "type": "select",
                    "default_value": "system",
                    "options": [
                        {"value": "system", "label": "System certificate authorities"},
                        {"value": "custom_ca", "label": "This source's private CA"},
                    ],
                    "value_updates": {"system": {"ca_certificate": ""}},
                },
                {
                    "name": "ca_certificate",
                    "label": "CA certificate (PEM)",
                    "type": "textarea",
                    "default_value": "",
                    "required": True,
                    "visible_when": {"field": "tls_trust", "equals": "custom_ca"},
                    "description": (
                        "Paste public CA certificates only, never private keys. "
                        "Hostname and certificate verification remain required. "
                        "Maximum 16 KiB."
                    ),
                },
                {
                    "name": "host",
                    "label": "IMAP host",
                    "type": "text",
                    "required": True,
                },
                {
                    "name": "security",
                    "label": "Transport security",
                    "type": "select",
                    "default_value": "tls",
                    "options": [
                        {"value": "tls", "label": "TLS"},
                        {"value": "starttls", "label": "STARTTLS"},
                    ],
                    "value_updates": {"tls": {"port": 993}, "starttls": {"port": 143}},
                },
                {
                    "name": "port",
                    "label": "Port",
                    "type": "number",
                    "default_value": 993,
                    "min": 1,
                    "max": 65535,
                },
                {
                    "name": "mailbox",
                    "label": "Mailbox",
                    "type": "text",
                    "default_value": "INBOX",
                },
                *(
                    {
                        "name": key,
                        "label": key.replace("_", " ").title(),
                        "type": "number",
                        "default_value": pair[0],
                        "min": 10 if key == "timeout_seconds" else 1,
                        "max": pair[1],
                    }
                    for key, pair in LIMITS.items()
                ),
            ),
            validate=validate_imap_config,
            factory=lambda config: None,
            instance_factory=lambda record, directory: IMAPSource(
                config=record["config"], token_path=token_path(directory, record["id"])
            ),
            connection_service="imap",
            connection_auth="password",
            required_capabilities=(
                "connector:imap:read",
                "network:fetch",
                "credential:use",
            ),
        )
    )
