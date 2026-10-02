"""Strict named Slack scans, isolated from the legacy best-effort reader.

Enumerate provider-available history and threads on each run. All results are
staged before indexing; neither retention nor permission omissions imply deletion.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from openjarvis.connectors._stubs import Document
from openjarvis.connectors.oauth import load_tokens
from openjarvis.connectors.slack_connector import _validate_user_token
from openjarvis.connectors.token_vault import TokenVault
from openjarvis.connectors.web_sources import (
    _PublicSource,
    _reject_constant,
    _strict_pairs,
)
from openjarvis.security.public_http import fetch_public_source

_ORIGIN = "https://slack.com"
SLACK_LIMITS = {
    "max_conversations": (200, 2000),
    "max_requests": (500, 2000),
    "max_documents": (5000, 10000),
    "timeout_seconds": (120, 300),
}
_METHODS = {
    "auth.test",
    "users.list",
    "conversations.list",
    "conversations.history",
    "conversations.replies",
}


def validate_slack_config(config):
    if set(config) - SLACK_LIMITS.keys():
        raise ValueError("Unknown Slack scan configuration field")
    result = {}
    for field, value in config.items():
        minimum = 10 if field == "timeout_seconds" else 1
        if type(value) is not int or not minimum <= value <= SLACK_LIMITS[field][1]:
            raise ValueError(f"{field} must be a bounded integer")
        result[field] = value
    return result


def _identity(value, prefixes):
    if not isinstance(value, str) or not re.fullmatch(
        f"[{prefixes}][A-Z0-9]{{1,63}}", value
    ):
        raise ValueError("Invalid Slack object identity")
    return value


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,12}\.[0-9]{6}", value):
        raise ValueError("Invalid Slack message timestamp")
    # Decimal preserves Slack's six-digit message identity and scan boundary.
    parsed = Decimal(value)
    if parsed <= 0:
        raise ValueError("Invalid Slack message timestamp")
    datetime.fromtimestamp(float(parsed), timezone.utc)  # Validate datetime range.
    return parsed


def _text(value, limit=4096):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("Invalid Slack text")
    return value


class SlackSource(_PublicSource):
    connector_id = "slack"
    auth_type = "managed"

    def __init__(self, *, credentials_path, config):
        super().__init__(config=config)
        self._credentials_path = Path(credentials_path)

    def is_connected(self):
        values = load_tokens(str(self._credentials_path)) or {}
        try:
            _validate_user_token(values.get("token", ""))
            return True
        except ValueError:
            return False
        finally:
            values.clear()

    def disconnect(self):
        TokenVault(self._credentials_path).delete()

    def sync(self, *, since=None, cursor=None):
        values = load_tokens(str(self._credentials_path)) or {}
        try:
            token = values.get("token", "")
            _validate_user_token(token)
            self._authentication = {
                "origin": _ORIGIN,
                "secret": token,
                "headers": {"Authorization": "Bearer " + token},
            }
            yield from super().sync(since=since, cursor=cursor)
        finally:
            values.clear()
            self._authentication = None

    def _check(self):
        self.check_sync_cancelled()
        if time.monotonic() >= self._deadline:
            raise ValueError("Slack scan exceeded its deadline")

    def _request(self, method, params):
        self._check()
        if method not in _METHODS or self._requests >= self.limits["max_requests"]:
            raise ValueError("Slack scan exceeded its request limit")
        self._requests += 1
        control = getattr(self, "_sync_control", None)
        if control:
            control.report(phase="fetching")
        url = _ORIGIN + "/api/" + method
        if params:
            url += "?" + urlencode(params)
        response = fetch_public_source(
            url,
            accept="application/json",
            authentication=self._authentication,
            allowed_origin=_ORIGIN,
            max_bytes=2 * 1024 * 1024,
            deadline=self._deadline,
            cancel_event=control,
        )
        self._check()
        if response.status_code != 200 or str(response.url) != url:
            raise ValueError("Slack rejected or redirected the read")
        self._bytes += len(response.content)
        if len(response.content) > 2 * 1024 * 1024 or self._bytes > 16 * 1024 * 1024:
            raise ValueError("Slack scan exceeded its response byte limit")
        self._reject_reflection(response.content.decode("utf-8", errors="replace"))
        value = json.loads(
            response.content,
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_constant,
        )
        self._reject_reflection(json.dumps(value, allow_nan=False, ensure_ascii=False))
        if not isinstance(value, dict) or value.get("ok") is not True:
            raise ValueError("Slack returned an unsuccessful read")
        return value

    def _collection(self, method, field, params, *, identity):
        cursor, seen_cursors, seen_items = "", set(), set()
        while True:
            value = self._request(
                method, {**params, **({"cursor": cursor} if cursor else {})}
            )
            items = value.get(field)
            if (
                not isinstance(items, list)
                or len(items) > params["limit"]
                or not all(isinstance(item, dict) for item in items)
            ):
                raise ValueError("Invalid Slack collection")
            if field == "messages":
                if type(value.get("has_more")) is not bool:
                    raise ValueError("Slack omitted message pagination status")
                limited = value.get("is_limited", False)
                if type(limited) is not bool:
                    raise ValueError("Invalid Slack retention status")
                if limited:
                    self._limited.add(params["channel"])
            metadata = value.get("response_metadata", {})
            if not isinstance(metadata, dict):
                raise ValueError("Invalid Slack response metadata")
            if field != "messages" and "next_cursor" not in metadata:
                raise ValueError("Slack omitted collection pagination status")
            next_cursor = metadata.get("next_cursor", "")
            if (
                not isinstance(next_cursor, str)
                or len(next_cursor) > 2048
                or any(ord(c) < 33 for c in next_cursor)
            ):
                raise ValueError("Invalid Slack pagination cursor")
            if next_cursor and next_cursor in seen_cursors:
                raise ValueError("Slack pagination repeated a cursor")
            if field == "messages" and value["has_more"] and not next_cursor:
                raise ValueError("Slack reported unfinished history without a cursor")
            # A nonempty next_cursor remains authoritative even on short/empty
            # pages or when has_more is false (Slack's pagination contracts).
            for item in items:
                self._check()
                key = identity(item)
                if key in seen_items:
                    raise ValueError("Slack pagination repeated a record")
                seen_items.add(key)
                yield item
            if not next_cursor:
                return
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def _message(self, message, channel, users, *, thread=None):
        ts = message.get("ts")
        numeric = _timestamp(ts)
        if numeric >= self._upper:
            raise ValueError("Slack message falls outside the scan")
        message_thread = message.get("thread_ts", ts)
        _timestamp(message_thread)
        if thread is not None and (
            message_thread != thread or numeric < _timestamp(thread)
        ):
            raise ValueError("Slack reply belongs to another thread")
        count = message.get("reply_count", 0)
        if type(count) is not int or not 0 <= count <= 1000000:
            raise ValueError("Invalid Slack reply count")
        if message.get("type") != "message":
            return None
        # Preserve human, bot and file/block-only messages; event subtypes are
        # represented as supplied by Slack rather than silently skipped.
        text = _text(message.get("text", ""), 1000000)
        extra = {
            key: message[key]
            for key in ("blocks", "attachments", "files")
            if key in message
        }
        content = text + ("\n\n" + json.dumps(extra, allow_nan=False) if extra else "")
        user_id = message.get("user", message.get("bot_id", ""))
        if user_id:
            _identity(user_id, "UWB")
        person = users.get(user_id, {})
        author = person.get("email") or person.get("name") or user_id
        channel_id = channel["id"]
        name = channel.get("name") or channel_id
        if channel.get("is_im"):
            peer = channel.get("user", "")
            name = "DM with " + (users.get(peer, {}).get("name") or peer or channel_id)
        identity = f"slack:{self._domain or self._team}:{channel_id}:{ts}"
        version = hashlib.sha256(
            json.dumps(message, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
        document = Document(
            doc_id=identity,
            source_id=identity,
            source="slack",
            doc_type="message",
            content=content,
            title=name if channel.get("is_im") else "#" + name,
            author=author,
            participants=[author.lower()] if author else [],
            participants_raw=[user_id] if user_id else [],
            timestamp=datetime.fromtimestamp(float(numeric), timezone.utc),
            thread_id=message_thread,
            channel=name,
            url=(
                f"https://{self._domain + '.' if self._domain else ''}slack.com"
                f"/archives/{channel_id}/p{ts.replace('.', '')}"
            ),
            metadata={
                "channel_id": channel_id,
                "channel_name": name,
                "team_id": self._team,
                "team_domain": self._domain,
                "user_id": user_id,
                "ts": ts,
                "fetched_at": self._started.isoformat(),
                "scan_upper_bound": str(self._upper),
                "coverage": "provider_available_conversation_history_and_threads",
                "content_version": version,
                "trust": "auto",
                "origin": "external",
            },
        )
        return document

    def _documents(self, *, since=None, cursor=None):
        config = validate_slack_config(self.config)
        self.limits = {
            key: config.get(key, pair[0]) for key, pair in SLACK_LIMITS.items()
        }
        self._requests = self._bytes = 0
        self._limited = set()
        self._started = datetime.now(timezone.utc)
        self._upper = Decimal(str(self._started.timestamp())).quantize(
            Decimal("0.000001")
        )
        self._deadline = time.monotonic() + self.limits["timeout_seconds"]
        auth = self._request("auth.test", {})
        self._team = _identity(auth.get("team_id"), "T")
        _identity(auth.get("user_id"), "UW")
        workspace = urlsplit(auth.get("url", ""))
        self._domain = ""
        if workspace.hostname and workspace.hostname.endswith(".slack.com"):
            domain = workspace.hostname[: -len(".slack.com")]
            if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", domain):
                self._domain = domain
        users = {}
        for member in self._collection(
            "users.list",
            "members",
            {"limit": 100},
            identity=lambda item: _identity(item.get("id"), "UWB"),
        ):
            if len(users) >= 10000:
                raise ValueError("Slack user directory exceeds its limit")
            profile = member.get("profile", {})
            if not isinstance(profile, dict):
                raise ValueError("Invalid Slack user profile")
            users[member["id"]] = {
                "name": _text(
                    member.get("real_name") or member.get("name") or member["id"]
                ),
                "email": _text(profile.get("email", "")),
            }
        channels = []
        for channel in self._collection(
            "conversations.list",
            "channels",
            {
                "limit": 100,
                "types": "public_channel,private_channel,mpim,im",
                "exclude_archived": "false",
            },
            identity=lambda item: _identity(item.get("id"), "CDG"),
        ):
            if len(channels) >= self.limits["max_conversations"]:
                raise ValueError("Slack scan exceeded its conversation limit")
            for flag in ("is_im", "is_mpim", "is_private", "is_archived"):
                if flag in channel and type(channel[flag]) is not bool:
                    raise ValueError("Invalid Slack conversation flags")
            if "name" in channel:
                _text(channel["name"])
            if channel.get("is_im") and "user" in channel:
                _identity(channel["user"], "UW")
            channels.append(channel)
        documents = {}

        def retain(message, channel, thread=None):
            doc = self._message(message, channel, users, thread=thread)
            if doc:
                if (
                    doc.doc_id not in documents
                    and len(documents) >= self.limits["max_documents"]
                ):
                    raise ValueError("Slack scan exceeded its document limit")
                documents[doc.doc_id] = doc

        for channel in channels:
            params = {
                "channel": channel["id"],
                "limit": 15,
                "latest": str(self._upper),
                "inclusive": "false",
            }
            threads = set()
            for message in self._collection(
                "conversations.history",
                "messages",
                params,
                identity=lambda item: str(_timestamp(item.get("ts"))),
            ):
                retain(message, channel)
                if (
                    message.get("reply_count", 0)
                    and message.get("thread_ts", message["ts"]) == message["ts"]
                ):
                    threads.add(message["ts"])
            for thread in sorted(threads):
                parent_seen = False
                for reply in self._collection(
                    "conversations.replies",
                    "messages",
                    {**params, "ts": thread},
                    identity=lambda item: str(_timestamp(item.get("ts"))),
                ):
                    retain(reply, channel, thread)
                    parent_seen = parent_seen or reply["ts"] == thread
                if not parent_seen:
                    raise ValueError("Slack thread read omitted its parent")
        self._check()
        for document in documents.values():
            document.metadata["provider_history_limited"] = (
                document.metadata["channel_id"] in self._limited
            )
        return list(documents.values())
