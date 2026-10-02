"""Shared bounded HTTP and staging for trusted named account readers."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from openjarvis.connectors._stubs import Document
from openjarvis.connectors.oauth import load_tokens
from openjarvis.connectors.sync_control import SyncLimitExceeded
from openjarvis.connectors.token_vault import TokenVault
from openjarvis.connectors.web_sources import (
    _PublicSource,
    _reject_constant,
    _strict_pairs,
)
from openjarvis.security.public_http import fetch_public_source

REMAINING_SERVICES = {
    "gmail",
    "gdrive",
    "gcalendar",
    "gcontacts",
    "google_tasks",
    "dropbox",
    "granola",
    "oura",
    "github_notifications",
    "weather",
}
LIMITS = {
    "max_requests": (500, 2000),
    "max_documents": (5000, 10000),
    "timeout_seconds": (120, 300),
}
ORIGINS = {
    "gmail": ("https://gmail.googleapis.com",),
    "gdrive": ("https://www.googleapis.com",),
    "gcalendar": ("https://www.googleapis.com",),
    "gcontacts": ("https://people.googleapis.com",),
    "google_tasks": ("https://tasks.googleapis.com",),
    "dropbox": ("https://api.dropboxapi.com", "https://content.dropboxapi.com"),
    "granola": ("https://public-api.granola.ai",),
    "oura": ("https://api.ouraring.com",),
    "github_notifications": ("https://api.github.com",),
    "weather": ("https://api.openweathermap.org",),
}


def validate_scan_config(service, config):
    allowed = (
        set(LIMITS)
        | ({"location"} if service == "weather" else set())
        | ({"lookback_days"} if service == "oura" else set())
    )
    if set(config) - allowed:
        raise ValueError("Unsupported account scan configuration")
    result = {}
    for field in set(config) & set(LIMITS):
        value = config[field]
        if (
            type(value) is not int
            or not (10 if field == "timeout_seconds" else 1)
            <= value
            <= LIMITS[field][1]
        ):
            raise ValueError(f"{field} must be a bounded integer")
        result[field] = value
    if service == "weather":
        location = config.get("location", "")
        if not isinstance(location, str) or not 1 <= len(location.strip()) <= 200:
            raise ValueError("A weather location is required")
        result["location"] = location.strip()
    if "lookback_days" in config:
        if (
            type(config["lookback_days"]) is not int
            or not 1 <= config["lookback_days"] <= 3650
        ):
            raise ValueError("Oura lookback must be between 1 and 3650 days")
        result["lookback_days"] = config["lookback_days"]
    return result


def identity(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 512
        or any(ord(c) < 33 for c in value)
    ):
        raise ValueError("Invalid provider identity or cursor")
    return value


def path_id(value):
    value = identity(value)
    if value in {".", ".."}:
        raise ValueError("Invalid provider path identity")
    return quote(value, safe="")


def timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Missing provider timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Provider timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def rows(payload, field, *, optional=False):
    if not isinstance(payload, dict) or "error" in payload:
        raise ValueError("Invalid provider response object")
    value = payload.get(field, [] if optional else None)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("Invalid provider collection")
    return value


class AccountScan(_PublicSource):
    def __init__(self, *, service, token_path, config):
        super().__init__(config=config)
        self.connector_id = service
        self._token_path = self._credentials_path = Path(token_path)
        self.origins = ORIGINS[service]

    def _token(self, values):
        field = (
            "access_token"
            if self.connector_id
            in {"gmail", "gdrive", "gcalendar", "gcontacts", "google_tasks"}
            else "api_key"
            if self.connector_id == "weather"
            else "token"
        )
        token = values.get(field)
        if not isinstance(token, str) or not token.strip():
            raise ValueError("Authorize this source before syncing")
        return token

    def is_connected(self):
        values = load_tokens(str(self._token_path)) or {}
        try:
            return bool(self._token(values))
        except ValueError:
            return False
        finally:
            values.clear()

    def disconnect(self):
        TokenVault(self._token_path).delete()

    def sync(self, *, since=None, cursor=None):
        values = load_tokens(str(self._token_path)) or {}
        try:
            token = self._token(values)
            self._authentication = {
                "origin": self.origins[0],
                "secret": token,
                "headers": {"Authorization": "Bearer " + token},
            }
            self.config = validate_scan_config(self.connector_id, self.config)
            self.limits = {
                key: self.config.get(key, pair[0]) for key, pair in LIMITS.items()
            }
            self.started = datetime.now(timezone.utc)
            self.deadline = time.monotonic() + self.limits["timeout_seconds"]
            self.requests = self.bytes = 0
            self.documents = {}
            # Provider inventories/windows are reread. Creation-time filters
            # cannot guarantee coverage of older edits or late arrivals.
            yield from super().sync(since=None, cursor=None)
        finally:
            values.clear()
            self._authentication = None

    def check(self):
        self.check_sync_cancelled()
        if time.monotonic() >= self.deadline:
            raise SyncLimitExceeded("deadline")

    def request(
        self,
        endpoint,
        params=None,
        *,
        body=None,
        origin=None,
        headers=None,
        raw=False,
        post=False,
    ):
        self.check()
        if self.requests >= self.limits["max_requests"]:
            raise SyncLimitExceeded("request")
        self.requests += 1
        origin = origin or self.origins[0]
        if (
            origin not in self.origins
            or not endpoint.startswith("/")
            or endpoint.startswith("//")
        ):
            raise ValueError("Untrusted account endpoint")
        url = origin + endpoint + ("?" + urlencode(params) if params else "")
        authentication = {
            **self._authentication,
            "origin": origin,
            "headers": {**self._authentication["headers"], **(headers or {})},
        }
        if body is not None:
            authentication["headers"]["Content-Type"] = "application/json"
        if self.connector_id == "weather":
            authentication["headers"] = {}
            authentication["query"] = {"appid": authentication["secret"]}
        control = getattr(self, "_sync_control", None)
        if control:
            control.report(phase="fetching")
        response = fetch_public_source(
            url,
            accept="application/octet-stream" if raw else "application/json",
            authentication=authentication,
            allowed_origin=origin,
            max_bytes=2 * 1024 * 1024,
            deadline=self.deadline,
            cancel_event=control,
            **(
                {"method": "POST", "body": json.dumps(body).encode()}
                if body is not None
                else {"method": "POST", "body": b""}
                if post
                else {}
            ),
        )
        self.check()
        if response.status_code != 200 or str(response.url) != url:
            raise ValueError("Provider rejected or redirected the read")
        self.bytes += len(response.content)
        if len(response.content) > 2 * 1024 * 1024 or self.bytes > 16 * 1024 * 1024:
            raise SyncLimitExceeded("bytes")
        self._reject_reflection(response.content.decode("utf-8", errors="replace"))
        if not raw:
            value = json.loads(
                response.content,
                object_pairs_hook=_strict_pairs,
                parse_constant=_reject_constant,
            )
            self._reject_reflection(
                json.dumps(value, ensure_ascii=False, allow_nan=False)
            )
            return value, response
        return response.content, response

    def collection(
        self,
        endpoint,
        field,
        params=None,
        *,
        key="id",
        optional=False,
        next_key="nextPageToken",
        cursor_param="pageToken",
    ):
        cursor, seen_cursors, seen = None, set(), set()
        while True:
            value, _ = self.request(
                endpoint,
                {**(params or {}), **({cursor_param: cursor} if cursor else {})},
            )
            if (
                optional
                and isinstance(value, dict)
                and field not in value
                and not (
                    isinstance(value.get("kind"), str)
                    or any(
                        type(value.get(key)) is int and value[key] == 0
                        for key in ("resultSizeEstimate", "totalPeople", "totalItems")
                    )
                )
            ):
                raise ValueError("Provider omitted its collection")
            for item in rows(value, field, optional=optional):
                self.check()
                item_id = identity(item.get(key))
                if item_id in seen:
                    raise ValueError("Provider pagination repeated a record")
                seen.add(item_id)
                yield item
            if next_key == "next_token" and next_key not in value:
                raise ValueError("Oura omitted pagination status")
            next_value = value.get(next_key)
            if next_value is None:
                return
            cursor = identity(next_value)
            if cursor in seen_cursors:
                raise ValueError("Provider pagination repeated a cursor")
            seen_cursors.add(cursor)

    def add(
        self,
        doc_id,
        item,
        *,
        title="",
        content=None,
        ts=None,
        doc_type="document",
        metadata=None,
        url=None,
    ):
        self.check()
        if doc_id in self.documents:
            raise ValueError("Provider scan repeated a document")
        if len(self.documents) >= self.limits["max_documents"]:
            raise SyncLimitExceeded("document")
        serialized = json.dumps(
            item, ensure_ascii=False, sort_keys=True, allow_nan=False
        )
        if content is not None and not isinstance(content, str):
            raise ValueError("Invalid provider content")
        if not isinstance(title, str):
            raise ValueError("Invalid provider title")
        if url is not None and (
            not isinstance(url, str) or not url.startswith("https://")
        ):
            raise ValueError("Invalid provider evidence URL")
        self.documents[doc_id] = Document(
            doc_id=doc_id,
            source_id=doc_id,
            source=self.connector_id,
            doc_type=doc_type,
            content=serialized if content is None else content,
            title=title,
            timestamp=ts or self.started,
            url=url,
            metadata={
                "fetched_at": self.started.isoformat(),
                "coverage": "provider_available_inventory",
                "timestamp_semantics": "provider_time" if ts else "observed_at",
                "content_version": hashlib.sha256(
                    (serialized + (content or "")).encode()
                ).hexdigest(),
                "trust": "auto",
                "origin": "external",
                **(metadata or {}),
            },
        )
