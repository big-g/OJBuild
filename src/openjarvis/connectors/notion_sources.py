"""Named Notion page readers; credentials are supplied only by SourceManager.

Search is not a complete workspace inventory, so missing pages never trigger
deletion reconciliation. Scan limits are errors, not successful partial syncs.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

from openjarvis.connectors._stubs import Document
from openjarvis.connectors.notion import (
    _extract_page_title,
    _render_blocks_to_markdown,
)
from openjarvis.connectors.web_sources import (
    _PublicSource,
    _reject_constant,
    _strict_pairs,
)
from openjarvis.security.public_http import fetch_public_source

NOTION_ORIGIN = "https://api.notion.com"
_API = NOTION_ORIGIN + "/v1"
_VERSION = "2022-06-28"  # Pinned page/block contract, independent of database APIs.


def validate_notion_config(config: dict) -> dict:
    reference = config.get("credential_id")
    try:
        reference = str(uuid.UUID(reference))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("A protected Notion bearer credential is required") from None
    query = config.get("query", "")
    if (
        not isinstance(query, str)
        or len(query) > 200
        or any(ord(char) < 32 for char in query)
    ):
        raise ValueError("Title filter must contain at most 200 printable characters")
    result = {"credential_id": reference, "query": query.strip()}
    for field, default, maximum in (
        ("max_pages", 100, 500),
        ("max_requests", 300, 1000),
        ("max_blocks", 5000, 20000),
    ):
        value = config.get(field, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"{field} must be an integer between 1 and {maximum}")
        result[field] = value
    return result


def _identity(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Notion returned an invalid object ID") from None


class NotionSource(_PublicSource):
    connector_id = "notion"
    display_name = "Notion pages"
    required_capabilities = (
        "connector:notion:read",
        "network:fetch",
        "credential:use",
    )

    def is_connected(self):
        return bool(self.config.get("credential_id") and self._authentication)

    def _begin(self):
        self.config = validate_notion_config(self.config)
        if not self._authentication or self._authentication["origin"] != NOTION_ORIGIN:
            raise ValueError("Notion requires its bound protected credential")
        self._deadline = time.monotonic() + 120
        self._requests = self._bytes = self._blocks = 0
        self._visited_blocks = set()

    def _request(self, endpoint, *, body=None):
        self.check_sync_cancelled()
        if time.monotonic() >= self._deadline:
            raise ValueError("Notion scan exceeded its two-minute deadline")
        self._requests += 1
        if self._requests > self.config["max_requests"]:
            raise ValueError("Notion scan exceeded its request limit")
        control = getattr(self, "_sync_control", None)
        if control:
            control.report(phase="fetching")
        authentication = {
            **self._authentication,
            "headers": {
                **self._authentication["headers"],
                "Notion-Version": _VERSION,
                "Content-Type": "application/json",
            },
        }
        response = fetch_public_source(
            _API + endpoint,
            accept="application/json",
            max_bytes=2 * 1024 * 1024,
            authentication=authentication,
            deadline=self._deadline,
            cancel_event=control,
            **(
                {"method": "POST", "body": json.dumps(body).encode()}
                if body is not None
                else {}
            ),
        )
        self.check_sync_cancelled()
        if time.monotonic() >= self._deadline:
            raise ValueError("Notion scan exceeded its two-minute deadline")
        if len(response.content) > 2 * 1024 * 1024:
            raise ValueError("Notion response exceeded its byte limit")
        if response.status_code != 200:
            raise ValueError("Notion rejected the read request")
        self._bytes += len(response.content)
        if self._bytes > 16 * 1024 * 1024:
            raise ValueError("Notion scan exceeded its response byte limit")
        self._reject_reflection(response.content.decode("utf-8", errors="replace"))
        value = json.loads(
            response.content,
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_constant,
        )
        if not isinstance(value, dict):
            raise ValueError("Notion returned an invalid response object")
        return value

    def _list(self, endpoint, *, body=None):
        cursor, seen = None, set()
        while True:
            query = {"page_size": 100}
            if cursor:
                query["start_cursor"] = cursor
            value = self._request(
                endpoint if body is not None else endpoint + "?" + urlencode(query),
                body={**body, **query} if body is not None else None,
            )
            results = value.get("results")
            if (
                not isinstance(results, list)
                or len(results) > 100
                or not all(isinstance(item, dict) for item in results)
                or type(value.get("has_more")) is not bool
            ):
                raise ValueError("Notion returned an invalid paginated response")
            yield from results
            if not value["has_more"]:
                return
            cursor = value.get("next_cursor")
            if (
                not isinstance(cursor, str)
                or not 1 <= len(cursor) <= 2048
                or any(ord(char) < 33 for char in cursor)
                or cursor in seen
            ):
                raise ValueError("Notion pagination did not advance")
            seen.add(cursor)

    def _content(self, parent, depth=0):
        if depth > 8 or parent in self._visited_blocks:
            raise ValueError("Notion block nesting exceeded its safe limit")
        self._visited_blocks.add(parent)
        lines = []
        for block in self._list(f"/blocks/{parent}/children"):
            identity = _identity(block.get("id"))
            self._blocks += 1
            if self._blocks > self.config["max_blocks"]:
                raise ValueError("Notion scan exceeded its block limit")
            if type(block.get("has_children")) is not bool:
                raise ValueError("Notion returned invalid block metadata")
            lines.append(_render_blocks_to_markdown([block]))
            if block["has_children"]:
                lines.append(self._content(identity, depth + 1))
        return "\n".join(line for line in lines if line)

    def _search_body(self):
        body = {"filter": {"property": "object", "value": "page"}}
        if self.config["query"]:
            body["query"] = self.config["query"]
        return body

    def probe(self):
        try:
            self._begin()
            value = self._request(
                "/search", body={**self._search_body(), "page_size": 1}
            )
            if (
                not isinstance(value.get("results"), list)
                or len(value["results"]) > 1
                or type(value.get("has_more")) is not bool
            ):
                raise ValueError("Invalid search response")
            return {"documents": len(value["results"])}
        except Exception:
            raise ValueError(
                "Notion connection test failed; check token and sharing"
            ) from None

    def _documents(self, *, since=None, cursor=None):
        self._begin()
        documents, seen = [], set()
        for page in self._list("/search", body=self._search_body()):
            identity = _identity(page.get("id"))
            if identity in seen or page.get("object") != "page":
                raise ValueError("Notion returned duplicate or invalid pages")
            seen.add(identity)
            if len(seen) > self.config["max_pages"]:
                raise ValueError("Notion scan exceeded its page limit")
            if page.get("archived") is True or page.get("in_trash") is True:
                continue
            edited = page.get("last_edited_time")
            if not isinstance(edited, str):
                raise ValueError("Notion page has no modification timestamp")
            timestamp = datetime.fromisoformat(edited.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError("Notion timestamp is missing a timezone")
            self._visited_blocks = set()
            content = self._content(identity)
            documents.append(
                Document(
                    doc_id=f"notion:{identity}",
                    source="notion",
                    doc_type="document",
                    content=content,
                    title=_extract_page_title(page),
                    timestamp=timestamp,
                    url=f"https://www.notion.so/{uuid.UUID(identity).hex}",
                    metadata={
                        "page_id": identity,
                        "provider_modified_at": edited,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                        "api_version": _VERSION,
                        "version": hashlib.sha256(content.encode()).hexdigest(),
                    },
                )
            )
        return documents
