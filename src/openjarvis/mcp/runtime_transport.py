"""Nonredirecting, bounded public HTTPS transport for web-managed MCP servers."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace

from openjarvis.mcp.protocol import MCPResponse
from openjarvis.mcp.transport import MCPTransport, StreamableHTTPTransport
from openjarvis.security.public_http import (
    _request_source,
    normalize_source_url,
    validate_public_url,
)


def endpoint(url):
    normalized = normalize_source_url(url)
    if not normalized.startswith("https://") or "?" in normalized:
        raise ValueError("Managed MCP requires a public HTTPS URL without a query")
    return normalized


class RuntimeHTTPTransport(MCPTransport):
    """Reuse pinned source connections; never execute a configured subprocess."""

    def __init__(self, url, token=""):
        self.url = endpoint(url)
        self._token = token
        self._session = None
        self._target = None
        self.protocol_version = None
        self._closed = threading.Event()
        self.deadline = time.monotonic() + 60

    def _post(self, request):
        if self._closed.is_set():
            raise ValueError("MCP connection is closed")
        body = request.to_json().encode()
        if len(body) > 65536:
            raise ValueError("MCP request exceeds 64 KiB")
        headers = {"Content-Type": "application/json"}
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        # Revalidate DNS and pin every request; no ambient proxy or redirect.
        target = validate_public_url(self.url)
        # Source reads may retry addresses. Tool calls may have side effects;
        # choose one pinned address so a lost response never repeats a POST.
        target = replace(target, addresses=target.addresses[:1])
        self._target = target
        response = _request_source(
            self.url,
            target,
            max_bytes=2 * 1024 * 1024,
            deadline=self.deadline,
            accept="application/json, text/event-stream",
            credential_headers=headers,
            method="POST",
            body=body,
            cancel_event=self._closed,
        )
        if response.status_code not in {200, 202, 204}:
            raise ValueError("MCP endpoint rejected the request")
        session = response.headers.get("mcp-session-id")
        if session is not None:
            if not 1 <= len(session) <= 1024 or any(
                ord(char) < 33 or ord(char) > 126 for char in session
            ):
                raise ValueError("MCP endpoint returned an invalid session")
            self._session = session
        return response

    def send(self, request):
        response = self._post(request)
        if response.status_code != 200:
            raise ValueError("MCP endpoint returned no response")
        body = response.text
        if "text/event-stream" in response.headers.get("content-type", ""):
            body = StreamableHTTPTransport._extract_json_from_sse(body)
        payload = json.loads(body)
        if not isinstance(payload, dict) or (
            payload.get("jsonrpc") != "2.0"
            or type(payload.get("id")) is not type(request.id)
            or payload.get("id") != request.id
            or ("result" in payload) == ("error" in payload)
            or ("error" in payload and not isinstance(payload["error"], dict))
        ):
            raise ValueError("MCP endpoint returned an invalid response")
        return MCPResponse.from_json(body)

    def send_notification(self, request):
        self._post(request)

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            if self._session and self._target and time.monotonic() < self.deadline:
                headers = {"Mcp-Session-Id": self._session}
                if self.protocol_version:
                    headers["MCP-Protocol-Version"] = self.protocol_version
                if self._token:
                    headers["Authorization"] = f"Bearer {self._token}"
                # Best-effort session termination on the last verified target.
                # No redirect or retry; cleanup cannot extend the run budget.
                _request_source(
                    self.url,
                    self._target,
                    method="DELETE",
                    max_bytes=0,
                    deadline=min(self.deadline, time.monotonic() + 3),
                    accept="application/json",
                    credential_headers=headers,
                )
        except Exception:
            pass
        self._token = ""
        self._session = None
        self._target = None
