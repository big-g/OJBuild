"""Public page and JSON GET readers for database-backed source instances."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Iterator

from openjarvis.connectors._stubs import BaseConnector, Document, SyncStatus
from openjarvis.core.registry import ConnectorRegistry
from openjarvis.security.public_http import fetch_public_source, normalize_source_url

_MAX_RECORDS = 1000


def validate_pointer(pointer: str) -> str:
    if not isinstance(pointer, str) or len(pointer) > 512:
        raise ValueError("JSON pointers must be strings of at most 512 characters")
    if pointer and not pointer.startswith("/"):
        raise ValueError("JSON pointers must be empty or start with '/'")
    if re.search(r"~(?![01])", pointer):
        raise ValueError("JSON pointer escapes must use ~0 or ~1")
    return pointer


def _pointer(value, pointer: str):
    for raw in pointer.split("/")[1:] if pointer else []:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", token):
            try:
                value = value[int(token)]
            except IndexError as exc:
                raise ValueError(f"JSON pointer does not exist: {pointer}") from exc
        else:
            raise ValueError(f"JSON pointer does not exist: {pointer}")
    return value


def validate_web_config(config: dict) -> dict:
    result = {"url": normalize_source_url(config.get("url", ""))}
    reference = config.get("credential_id", "")
    if reference:
        import uuid

        if not isinstance(reference, str):
            raise ValueError("Credential reference must be a UUID")
        try:
            result["credential_id"] = str(uuid.UUID(reference))
        except ValueError:
            raise ValueError("Credential reference must be a UUID") from None
    elif reference not in ("", None):
        raise ValueError("Credential reference must be a UUID")
    return result


def validate_json_config(config: dict) -> dict:
    result = validate_web_config(config)
    mode = config.get("mode", "document")
    if not isinstance(mode, str) or mode not in {"document", "records"}:
        raise ValueError("JSON mode must be document or records")
    limit = config.get("max_records", 200)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= _MAX_RECORDS
    ):
        raise ValueError("Record limit must be an integer between 1 and 1000")
    complete = config.get("complete_snapshot", False)
    if not isinstance(complete, bool):
        raise ValueError("Complete snapshot must be a boolean")
    result.update(mode=mode, max_records=limit, complete_snapshot=complete)
    for field, default in (
        ("records_pointer", ""),
        ("id_pointer", "/id"),
        ("title_pointer", ""),
        ("content_pointer", ""),
    ):
        result[field] = validate_pointer(config.get(field, default))
    if mode == "records" and not result["id_pointer"]:
        raise ValueError("Record mode requires a stable ID pointer")
    return result


class _PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self.stack: list[tuple[str, bool]] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        hidden = (
            tag in {"script", "style", "template"}
            or "hidden" in values
            or values.get("aria-hidden") == "true"
        )
        inherited = self.stack[-1][1] if self.stack else False
        if tag not in {
            "br",
            "hr",
            "img",
            "input",
            "meta",
            "link",
            "area",
            "source",
            "wbr",
            "embed",
            "base",
            "col",
            "param",
            "track",
        }:
            self.stack.append((tag, inherited or hidden))
        if not inherited and tag in {
            "p",
            "div",
            "br",
            "li",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "tr",
            "section",
            "article",
        }:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.stack and self.stack[-1][1]:
            return
        if any(tag == "title" for tag, _ in self.stack):
            self.title.append(data)
        else:
            self.parts.append(data)

    def content(self):
        return "\n".join(
            line
            for part in "".join(self.parts).splitlines()
            if (line := " ".join(part.split()))
        )


def _json_text(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON contains duplicate object key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"JSON contains nonstandard number: {value}")


class _PublicSource(BaseConnector):
    auth_type = "managed"

    def __init__(self, *, config: dict | None = None):
        self.config = config or {}
        self._status = SyncStatus()
        self._authentication = None

    def bind_credential(self, material: dict) -> None:
        self._authentication = material

    def is_connected(self) -> bool:
        return bool(self.config.get("url"))

    def disconnect(self) -> None:
        self.config = {}

    def sync_status(self) -> SyncStatus:
        return self._status

    def _documents(self) -> list[Document]:
        raise NotImplementedError

    def sync(self, *, since=None, cursor=None) -> Iterator[Document]:
        self._status = SyncStatus(state="syncing")
        try:
            if self.config.get("credential_id") and not self._authentication:
                raise ValueError(
                    "Source credential must be unlocked through SourceManager"
                )
            documents = self._documents()
            if self._authentication:
                self._reject_reflection(
                    json.dumps(
                        [asdict(document) for document in documents], default=str
                    )
                )
            self._status.items_total = len(documents)
            for document in documents:
                yield document
                self._status.items_synced += 1
            self._status.state = "idle"
            self._status.last_sync = datetime.now(timezone.utc)
        except Exception as exc:
            self._status.state = "error"
            if self._authentication:
                self._status.error = (
                    "Authenticated source fetch or parsing failed; "
                    "check credentials, scope and response"
                )
                raise ValueError(self._status.error) from None
            self._status.error = str(exc)
            raise

    def _reject_reflection(self, text: str):
        import base64
        import html
        from urllib.parse import quote

        secret = self._authentication["secret"]
        variants = (
            secret,
            quote(secret, safe=""),
            html.escape(secret),
            json.dumps(secret)[1:-1],
            base64.b64encode(secret.encode()).decode(),
        )
        if any(value in text for value in variants):
            raise ValueError("Authenticated response reflects protected credentials")

    def _fetch(self, accept: str):
        response = fetch_public_source(
            self.config["url"],
            accept=accept,
            **(
                {"authentication": self._authentication} if self._authentication else {}
            ),
        )
        if self._authentication:
            self._reject_reflection(response.content.decode("utf-8", errors="replace"))
        if len(response.content) > 2 * 1024 * 1024:
            raise ValueError("Source response exceeds the 2 MiB limit")
        fetched_at = datetime.now(timezone.utc)
        metadata = {
            "trust": "auto",
            "origin": "external",
            "requested_url": self.config["url"],
            "final_url": str(response.url),
            "fetched_at": fetched_at.isoformat(),
            "response_version": hashlib.sha256(response.content).hexdigest(),
            "content_type": response.headers.get("content-type", ""),
        }
        if self._authentication:
            self._reject_reflection(json.dumps(metadata))
        return response, fetched_at, metadata


@ConnectorRegistry.register("web_page")
class WebPageConnector(_PublicSource):
    connector_id = "web_page"
    display_name = "Web Page"
    required_capabilities = ("connector:web_page:read", "network:fetch")

    def _documents(self) -> list[Document]:
        self.config = validate_web_config(self.config)
        response, fetched_at, metadata = self._fetch("text/html, text/plain;q=0.9")
        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if mime not in {
            "text/html",
            "application/xhtml+xml",
            "text/plain",
            "text/markdown",
        }:
            raise ValueError("Web Page requires HTML or plain text content")
        # Decode declared text encoding strictly instead of replacing corrupt bytes.
        text = response.content.decode(response.encoding or "utf-8")
        title = str(response.url)
        if mime in {"text/html", "application/xhtml+xml"}:
            parser = _PageText()
            parser.feed(text)
            parser.close()
            text = parser.content()
            title = " ".join("".join(parser.title).split()) or title
        if not text.strip():
            raise ValueError("Web Page returned no readable text")
        source_id = hashlib.sha256(self.config["url"].encode()).hexdigest()
        return [
            Document(
                doc_id=f"web_page:{source_id}",
                source_id=source_id,
                source="web_page",
                doc_type="document",
                title=title,
                content=text,
                url=str(response.url),
                timestamp=fetched_at,
                metadata={**metadata, "version": metadata["response_version"]},
            )
        ]


@ConnectorRegistry.register("json_api")
class JsonAPIConnector(_PublicSource):
    connector_id = "json_api"
    display_name = "JSON API"
    required_capabilities = ("connector:json_api:read", "network:fetch")

    def _documents(self) -> list[Document]:
        self.config = validate_json_config(self.config)
        response, fetched_at, metadata = self._fetch("application/json")
        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if mime != "application/json" and not (
            mime.startswith("application/") and mime.endswith("+json")
        ):
            raise ValueError("JSON API requires an application/json response")
        try:
            value = json.loads(
                response.content,
                object_pairs_hook=_strict_pairs,
                parse_constant=_reject_constant,
            )
            # Reject non-finite values including 1e999, not just literal NaN.
            _json_text(value)
        except (RecursionError, UnicodeError, ValueError) as exc:
            raise ValueError(f"JSON API returned invalid JSON: {exc}") from exc
        endpoint_id = hashlib.sha256(self.config["url"].encode()).hexdigest()
        if self.config["mode"] == "document":
            records = [(endpoint_id, str(response.url), _json_text(value), "")]
        else:
            rows = _pointer(value, self.config["records_pointer"])
            if not isinstance(rows, list):
                raise ValueError("Records pointer must select a JSON array")
            if len(rows) > self.config["max_records"]:
                raise ValueError("JSON API exceeds the configured record limit")
            records = []
            seen = set()
            for row in rows:
                record_id = _pointer(row, self.config["id_pointer"])
                if (
                    isinstance(record_id, bool)
                    or not isinstance(record_id, (str, int))
                    or record_id == ""
                ):
                    raise ValueError("Record IDs must be nonempty strings or integers")
                # Preserve scalar type: integer 1 and string '1' are distinct IDs.
                identity = _json_text(record_id)
                if identity in seen:
                    raise ValueError("JSON API returned duplicate record IDs")
                seen.add(identity)
                title = (
                    _pointer(row, self.config["title_pointer"])
                    if self.config["title_pointer"]
                    else str(record_id)
                )
                if not isinstance(title, (str, int, float)) or isinstance(title, bool):
                    raise ValueError("Title pointer must select a string or number")
                body = _pointer(row, self.config["content_pointer"])
                text = body if isinstance(body, str) else _json_text(body)
                native_id = (
                    endpoint_id + ":" + hashlib.sha256(identity.encode()).hexdigest()
                )
                records.append((native_id, str(title), text, record_id))
        documents = []
        for native_id, title, text, record_id in records:
            version = hashlib.sha256(text.encode()).hexdigest()
            documents.append(
                Document(
                    doc_id=f"json_api:{native_id}",
                    source_id=native_id,
                    source="json_api",
                    doc_type="document",
                    title=title,
                    content=text,
                    url=str(response.url),
                    timestamp=fetched_at,
                    metadata={
                        **metadata,
                        "version": version,
                        "record_id": record_id,
                        "records_pointer": self.config["records_pointer"],
                    },
                )
            )
        return documents


def probe_public_source(reader) -> dict:
    documents = list(reader.sync())
    return {
        "documents": len(documents),
        "sample_titles": [document.title for document in documents[:3]],
        "final_url": documents[0].url if documents else reader.config["url"],
    }
