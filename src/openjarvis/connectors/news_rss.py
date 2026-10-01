"""News/RSS connector — aggregate headlines from RSS and Atom feeds.

Uses stdlib xml.etree.ElementTree for parsing (no extra dependencies).
Config file lists feeds to follow. All HTTP calls are in module-level
functions for easy mocking in tests.
"""

from __future__ import annotations

import http.client
import json
import socket as socket  # re-export the existing transport test hook
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urljoin, urlparse

import httpx

from openjarvis.connectors._stubs import BaseConnector, Document, SyncStatus
from openjarvis.core.config import DEFAULT_CONFIG_DIR
from openjarvis.core.registry import ConnectorRegistry
from openjarvis.security.public_http import (
    PinnedHTTPConnection as _PinnedHTTPConnection,
)
from openjarvis.security.public_http import (
    PinnedHTTPSConnection as _PinnedHTTPSConnection,
)
from openjarvis.security.public_http import (
    PublicTarget,
    resolve_public_addresses,
    validate_public_url,
)

_DEFAULT_CONFIG_PATH = str(DEFAULT_CONFIG_DIR / "connectors" / "news_rss.json")
_MAX_REDIRECTS = 5
_MAX_FEED_BYTES = 10 * 1024 * 1024


# Compatibility wrappers retain the feed-specific test/extension hooks.
def _resolve_host_addresses(hostname: str, port: int) -> tuple[str, ...]:
    return resolve_public_addresses(hostname, port)


def _validate_feed_url(url: str) -> PublicTarget:
    return validate_public_url(url, resolver=_resolve_host_addresses)


def _request_feed(url: str, target: PublicTarget) -> httpx.Response:
    """GET *url* using only the already-validated addresses in *target*."""
    request = httpx.Request("GET", url)
    last_error: Exception | None = None

    for address in target.addresses:
        connection_cls = (
            _PinnedHTTPSConnection
            if target.scheme == "https"
            else _PinnedHTTPConnection
        )
        connection = connection_cls(
            target.hostname,
            target.port,
            pinned_ip=address,
            timeout=30.0,
        )
        try:
            connection.request(
                "GET",
                target.request_target,
                headers={
                    "Host": target.host_header,
                    "Accept": (
                        "application/rss+xml, application/atom+xml, "
                        "application/xml, text/xml;q=0.9, */*;q=0.1"
                    ),
                    "User-Agent": "OpenJarvis-RSS/1.0",
                    "Connection": "close",
                },
            )
            raw_response = connection.getresponse()
            body = raw_response.read(_MAX_FEED_BYTES + 1)
            if len(body) > _MAX_FEED_BYTES:
                raise ValueError(f"RSS feed exceeded {_MAX_FEED_BYTES} response bytes")
            return httpx.Response(
                raw_response.status,
                headers=raw_response.getheaders(),
                content=body,
                request=request,
            )
        except (OSError, http.client.HTTPException) as exc:
            last_error = exc
        finally:
            connection.close()

    raise httpx.RequestError(
        f"Unable to fetch RSS feed from its verified addresses: {last_error}",
        request=request,
    ) from last_error


def _fetch_feed(url: str) -> str:
    """Download feed XML while validating every redirect target."""
    current_url = url
    for _ in range(_MAX_REDIRECTS + 1):
        target = _validate_feed_url(current_url)
        resp = _request_feed(current_url, target)
        if resp.status_code not in {301, 302, 303, 307, 308}:
            resp.raise_for_status()
            return resp.text
        location = resp.headers.get("location", "")
        if not location:
            resp.raise_for_status()
            return resp.text
        current_url = urljoin(current_url, location)
    raise ValueError(f"RSS feed exceeded {_MAX_REDIRECTS} redirects")


def _parse_rss_items(xml_text: str, max_items: int = 5) -> List[Dict[str, str]]:
    """Parse RSS or Atom XML and return up to *max_items* entries."""
    root = ET.fromstring(xml_text)
    items: List[Dict[str, str]] = []

    # RSS 2.0: <rss><channel><item>
    for item_el in root.iter("item"):
        if len(items) >= max_items:
            break
        items.append(
            {
                "title": (item_el.findtext("title") or "").strip(),
                "description": (item_el.findtext("description") or "").strip()[:200],
                "link": (item_el.findtext("link") or "").strip(),
                "pubDate": (item_el.findtext("pubDate") or "").strip(),
            }
        )

    # Atom: <feed><entry>
    if not items:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry_el in root.iter("{http://www.w3.org/2005/Atom}entry"):
            if len(items) >= max_items:
                break
            link_el = entry_el.find("atom:link", ns)
            link_href = link_el.get("href", "") if link_el is not None else ""
            summary = entry_el.findtext("{http://www.w3.org/2005/Atom}summary") or ""
            updated = entry_el.findtext("{http://www.w3.org/2005/Atom}updated") or ""
            items.append(
                {
                    "title": (
                        entry_el.findtext("{http://www.w3.org/2005/Atom}title") or ""
                    ).strip(),
                    "description": summary.strip()[:200],
                    "link": link_href.strip(),
                    "pubDate": updated.strip(),
                }
            )

    return items


def _parse_pub_date(date_str: str) -> Optional[datetime]:
    """Best-effort parse of an RSS pubDate or Atom updated timestamp."""
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str)
    except (ValueError, TypeError):
        pass
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


@ConnectorRegistry.register("news_rss")
class NewsRSSConnector(BaseConnector):
    """Aggregate headlines from configured RSS/Atom feeds."""

    connector_id = "news_rss"
    display_name = "News / RSS"
    auth_type = "local"

    def __init__(self, *, config_path: str = _DEFAULT_CONFIG_PATH) -> None:
        self._config_path = Path(config_path)
        self._status = SyncStatus()

    def _load_config(self) -> List[Dict[str, str]]:
        """Load feed list from disk."""
        data = json.loads(self._config_path.read_text(encoding="utf-8"))
        return data.get("feeds", [])

    def configure(self, feeds: List[Dict[str, Any]]) -> None:
        """Validate and persist RSS feed configuration from the connect UI."""
        normalized: List[Dict[str, str]] = []
        for feed in feeds:
            if not isinstance(feed, dict):
                continue
            url = str(feed.get("url", "")).strip()
            _validate_feed_url(url)
            parsed = urlparse(url)
            name = str(feed.get("name", "")).strip() or parsed.netloc
            normalized.append({"name": name, "url": url})
        if not normalized:
            raise ValueError("At least one RSS feed URL is required")
        from openjarvis.security.file_utils import secure_write_json

        secure_write_json(self._config_path, {"feeds": normalized})

    def is_connected(self) -> bool:
        if not self._config_path.exists():
            return False
        try:
            feeds = self._load_config()
            return len(feeds) > 0
        except (json.JSONDecodeError, OSError):
            return False

    def disconnect(self) -> None:
        if self._config_path.exists():
            self._config_path.unlink()

    def sync(
        self, *, since: Optional[datetime] = None, cursor: Optional[str] = None
    ) -> Iterator[Document]:
        """Yield Documents for recent items across all configured feeds."""
        feeds = self._load_config()

        for feed in feeds:
            feed_name = feed.get("name", "Unknown Feed")
            feed_url = feed.get("url", "")
            if not feed_url:
                continue

            try:
                xml_text = _fetch_feed(feed_url)
            except (httpx.HTTPError, ValueError):
                continue

            items = _parse_rss_items(xml_text)
            for item in items:
                pub_dt = _parse_pub_date(item["pubDate"])

                # Filter by since if the date is parseable
                if since and pub_dt and pub_dt.replace(tzinfo=None) < since:
                    continue

                title = item["title"] or "Untitled"
                doc_id = f"rss-{feed_name}-{title[:40]}"

                yield Document(
                    doc_id=doc_id,
                    source="news_rss",
                    doc_type="article",
                    content=item["description"],
                    title=title,
                    timestamp=pub_dt or datetime.now(),
                    url=item["link"] or None,
                    metadata={"feed_name": feed_name},
                )

        self._status.state = "idle"
        self._status.last_sync = datetime.now()

    def sync_status(self) -> SyncStatus:
        return self._status
