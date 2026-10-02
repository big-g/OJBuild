"""Bounded named Spotify/Strava scans; a partial traversal is never success.

Spotify covers provider-available recent history. Strava enumerates accessible
activities on every scan because activity dates are not modification dates.
Neither provider's omissions authorize deletion of previously indexed evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from openjarvis.connectors._stubs import Document
from openjarvis.connectors.oauth import load_tokens, require_access_token
from openjarvis.connectors.token_vault import TokenVault
from openjarvis.connectors.web_sources import (
    _PublicSource,
    _reject_constant,
    _strict_pairs,
)
from openjarvis.security.public_http import fetch_public_source

SCAN_LIMITS = {
    "max_pages": (100, 250),
    "max_documents": (5000, 10000),
    "timeout_seconds": (120, 300),
}


def validate_activity_config(config):
    if set(config) - SCAN_LIMITS.keys():
        raise ValueError("Unknown activity scan configuration field")
    result = {}
    for field, value in config.items():
        minimum = 10 if field == "timeout_seconds" else 1
        if type(value) is not int or not minimum <= value <= SCAN_LIMITS[field][1]:
            raise ValueError(f"{field} must be a bounded integer")
        result[field] = value
    return result


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid provider timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Provider timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _text(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("Invalid provider text")
    return value


class ActivitySource(_PublicSource):
    auth_type = "oauth"

    def __init__(self, *, service, token_path, config):
        super().__init__(config=config)
        self.connector_id = service
        self._token_path = Path(token_path)
        self.origin, self.endpoint = (
            ("https://api.spotify.com", "/v1/me/player/recently-played")
            if service == "spotify"
            else ("https://www.strava.com", "/api/v3/athlete/activities")
        )

    def is_connected(self):
        values = load_tokens(str(self._token_path)) or {}
        try:
            return bool(require_access_token(values))
        except ValueError:
            return False
        finally:
            values.clear()

    def disconnect(self):
        TokenVault(self._token_path).delete()

    def sync(self, *, since=None, cursor=None):
        values = load_tokens(str(self._token_path)) or {}
        try:
            token = require_access_token(values)
            self._authentication = {
                "origin": self.origin,
                "secret": token,
                "headers": {"Authorization": "Bearer " + token},
            }
            yield from super().sync(since=since, cursor=cursor)
        finally:
            values.clear()
            self._authentication = None

    def _request(self, params):
        self.check_sync_cancelled()
        if self._requests >= self.limits["max_pages"]:
            raise ValueError("Activity scan exceeded its page limit")
        if time.monotonic() >= self._deadline:
            raise ValueError("Activity scan exceeded its deadline")
        self._requests += 1
        control = getattr(self, "_sync_control", None)
        if control:
            control.report(phase="fetching")
        url = self.origin + self.endpoint + "?" + urlencode(params)
        response = fetch_public_source(
            url,
            accept="application/json",
            authentication=self._authentication,
            allowed_origin=self.origin,
            max_bytes=2 * 1024 * 1024,
            deadline=self._deadline,
            cancel_event=control,
        )
        self.check_sync_cancelled()
        if time.monotonic() >= self._deadline:
            raise ValueError("Activity scan exceeded its deadline")
        if response.status_code != 200 or str(response.url) != url:
            raise ValueError("Provider rejected or redirected the activity read")
        self._bytes += len(response.content)
        if len(response.content) > 2 * 1024 * 1024 or self._bytes > 16 * 1024 * 1024:
            raise ValueError("Activity scan exceeded its response byte limit")
        self._reject_reflection(response.content.decode("utf-8", errors="replace"))
        value = json.loads(
            response.content,
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_constant,
        )
        # Also reject overflowed JSON floats, including in fields not indexed.
        json.dumps(value, allow_nan=False)
        return value

    def _append(self, documents, document):
        self.check_sync_cancelled()
        if time.monotonic() >= self._deadline:
            raise ValueError("Activity scan exceeded its deadline")
        if document.doc_id in self._seen:
            raise ValueError("Activity pagination repeated a record")
        self._seen.add(document.doc_id)
        document.source_id = document.doc_id
        if len(documents) >= self.limits["max_documents"]:
            raise ValueError("Activity scan exceeded its document limit")
        document.metadata.update(
            fetched_at=self._started.isoformat(),
            content_version=hashlib.sha256(document.content.encode()).hexdigest(),
            scan_upper_bound=self._started.isoformat(),
            origin="external",
            trust="auto",
        )
        documents.append(document)

    def _documents(self, *, since=None, cursor=None):
        config = validate_activity_config(self.config)
        self.limits = {
            field: config.get(field, pair[0]) for field, pair in SCAN_LIMITS.items()
        }
        self._requests = self._bytes = 0
        self._seen = set()
        self._started = datetime.now(timezone.utc)
        self._deadline = time.monotonic() + self.limits["timeout_seconds"]
        documents = (
            self._spotify(since) if self.connector_id == "spotify" else self._strava()
        )
        self.check_sync_cancelled()
        if time.monotonic() >= self._deadline:
            raise ValueError("Activity scan exceeded its deadline")
        return documents

    def _spotify(self, since):
        lower = (since or self._started - timedelta(days=1)) - timedelta(seconds=1)
        if lower.tzinfo is None:
            lower = lower.replace(tzinfo=timezone.utc)
        before = int(self._started.timestamp() * 1000) + 1
        documents = []
        while True:
            payload = self._request({"limit": 50, "before": before})
            if not isinstance(payload, dict) or "next" not in payload:
                raise ValueError("Invalid Spotify page")
            items = payload.get("items")
            if not isinstance(items, list) or len(items) > 50:
                raise ValueError("Invalid Spotify items")
            times = []
            for item in items:
                self.check_sync_cancelled()
                if not isinstance(item, dict) or not isinstance(
                    item.get("track"), dict
                ):
                    raise ValueError("Invalid Spotify play")
                played = _timestamp(item.get("played_at"))
                if int(played.timestamp() * 1000) >= before:
                    raise ValueError("Spotify history did not advance")
                times.append(played)
                track = item["track"]
                identity = track.get("id")
                if not isinstance(identity, str) or not re.fullmatch(
                    r"[A-Za-z0-9]{1,128}", identity
                ):
                    uri = track.get("uri")
                    if (
                        identity is not None
                        or not isinstance(uri, str)
                        or not uri.startswith("spotify:local:")
                    ):
                        raise ValueError("Invalid Spotify track identity")
                    identity = "local-" + hashlib.sha256(uri.encode()).hexdigest()
                name = _text(track.get("name"))
                artists = track.get("artists")
                if not isinstance(artists, list) or not all(
                    isinstance(a, dict) for a in artists
                ):
                    raise ValueError("Invalid Spotify artists")
                author = ", ".join(_text(a.get("name")) for a in artists)
                if played >= lower:
                    self._append(
                        documents,
                        Document(
                            doc_id=f"spotify-{identity}-{item['played_at']}",
                            source="spotify",
                            doc_type="recently_played",
                            content=json.dumps(item, allow_nan=False),
                            title=name + " — " + author,
                            author=author,
                            timestamp=played,
                            metadata={
                                "track_name": name,
                                "coverage": "provider_available_recent_history",
                                "window_start": lower.isoformat(),
                            },
                        ),
                    )
            next_url = payload["next"]
            if next_url is None:
                return documents
            if not isinstance(next_url, str) or len(next_url) > 2048:
                raise ValueError("Invalid Spotify next page")
            parsed = urlsplit(next_url)
            pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
            query = dict(pairs)
            if (
                parsed.scheme != "https"
                or parsed.netloc != "api.spotify.com"
                or parsed.path != self.endpoint
                or parsed.fragment
                or len(query) != len(pairs)
                or set(query) != {"limit", "before"}
                or query["limit"] != "50"
                or not re.fullmatch(r"[0-9]{1,16}", query["before"])
                or not 0 < int(query["before"]) < before
                or not items
            ):
                raise ValueError("Spotify pagination did not advance safely")
            next_before = int(query["before"])
            # Do not accept a cursor that skips beyond the oldest returned play.
            if next_before != min(int(value.timestamp() * 1000) for value in times):
                raise ValueError("Spotify cursor does not match its page")
            if all(value < lower for value in times):
                return documents
            before = next_before

    def _strava(self):
        documents, page = [], 1
        before = int(self._started.timestamp()) + 1
        while True:
            items = self._request({"per_page": 50, "page": page, "before": before})
            if not isinstance(items, list) or len(items) > 50:
                raise ValueError("Invalid Strava activities page")
            if not items:
                return documents
            for item in items:
                if (
                    not isinstance(item, dict)
                    or type(item.get("id")) is not int
                    or not 0 < item["id"] < 2**63
                ):
                    raise ValueError("Invalid Strava activity identity")
                started = _timestamp(item.get("start_date"))
                if started.timestamp() >= before:
                    raise ValueError("Strava returned an activity outside the scan")
                name = _text(item.get("name"))
                sport = _text(item.get("sport_type", item.get("type")))
                self._append(
                    documents,
                    Document(
                        doc_id=f"strava-{item['id']}",
                        source="strava",
                        doc_type=sport.lower(),
                        content=json.dumps(item, allow_nan=False),
                        title=name,
                        timestamp=started,
                        url=f"https://www.strava.com/activities/{item['id']}",
                        metadata={
                            "distance_m": item.get("distance"),
                            "moving_time_s": item.get("moving_time"),
                            "sport_type": sport,
                            "coverage": "accessible_activities",
                            "provider_activity_at": started.isoformat(),
                        },
                    ),
                )
            page += 1
