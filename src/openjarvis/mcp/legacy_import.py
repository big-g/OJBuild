"""Review and import server-owned legacy MCP configuration without connecting."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections import Counter
from urllib.parse import quote, unquote

from openjarvis.connectors.source_credentials import _secret
from openjarvis.core.config import get_config_dir, resolve_mcp_servers
from openjarvis.mcp.runtime_store import canonical, definition
from openjarvis.tools.runtime_store import RuntimeToolConflict

MAX_ENTRIES = 128
MAX_CONFIG_BYTES = 256 * 1024


class LegacyMCPImporter:
    def __init__(self, manager):
        self.manager = manager
        # Review handles are opaque, credential-bound and invalid after restart.
        self._key = secrets.token_bytes(32)

    def entries(self, raw):
        try:
            if isinstance(raw, str):
                if len(raw.encode()) > MAX_CONFIG_BYTES:
                    raise ValueError
                items = resolve_mcp_servers(raw, get_config_dir())
            else:
                items = json.loads(canonical(raw)) if raw is not None else []
            if not isinstance(items, list) or len(items) > MAX_ENTRIES:
                raise ValueError
            if len(canonical(items).encode()) > MAX_CONFIG_BYTES:
                raise ValueError
            return items
        except (ValueError, TypeError, OSError, RecursionError):
            raise ValueError(
                "Legacy MCP configuration is invalid or exceeds review limits"
            ) from None

    def digest(self, index, item):
        return hmac.new(
            self._key, canonical([index, item]).encode(), hashlib.sha256
        ).hexdigest()

    @staticmethod
    def candidate(item):
        if not isinstance(item, dict):
            raise ValueError("Entry must be a connection object")
        if set(item) - {
            "name",
            "url",
            "token",
            "command",
            "args",
            "include_tools",
            "exclude_tools",
        }:
            raise ValueError("Unsupported settings; configure this connection manually")
        if item.get("command") or item.get("args"):
            raise ValueError(
                "Local commands require the future package-installation workflow"
            )
        if item.get("include_tools") or item.get("exclude_tools"):
            raise ValueError(
                "Tool filters cannot be broadened by import; configure manually"
            )
        config = definition({"name": item.get("name"), "url": item.get("url")})
        token = item.get("token")
        if token is None:
            token = ""
        if not isinstance(token, str):
            raise ValueError("Invalid credential; configure this connection manually")
        if token:
            try:
                _secret(token)
            except ValueError:
                raise ValueError(
                    "Invalid credential; configure this connection manually"
                ) from None
            decoded_url = config["url"]
            values = [config["name"], decoded_url]
            for _ in range(3):
                decoded_url = unquote(decoded_url)
                values.append(decoded_url)
            if any(
                token in value or quote(token, safe="") in value for value in values
            ):
                raise ValueError(
                    "Credential embedded in connection metadata; configure manually"
                )
        return config, token

    def review(self, raw):
        return self._review(self.entries(raw))

    def _review(self, items):
        names = Counter(
            item.get("name")
            for item in items
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        )
        existing = {row["name"] for row in self.manager.store.list()}
        result = []
        for index, item in enumerate(items):
            row = {
                "index": index,
                "label": f"Legacy entry {index + 1}",
                "status": "blocked",
                "reason": "",
                "review_digest": self.digest(index, item),
            }
            try:
                config, token = self.candidate(item)
                if names[config["name"]] > 1:
                    raise ValueError(
                        "Duplicate legacy connection name; resolve before importing"
                    )
                row.update(
                    name=config["name"], url=config["url"], has_token=bool(token)
                )
                row["status"] = (
                    "already_saved" if config["name"] in existing else "ready"
                )
                row["reason"] = (
                    "Name already saved; existing connection is unchanged"
                    if row["status"] == "already_saved"
                    else ""
                )
            except (ValueError, TypeError, KeyError):
                # Never expose raw settings, commands or parser errors.
                row["reason"] = (
                    "Unsupported or unsafe legacy settings; configure manually"
                )
            result.append(row)
        return result

    def import_one(self, raw, index, review_digest, actor):
        items = self.entries(raw)
        if not 0 <= index < len(items) or not hmac.compare_digest(
            self.digest(index, items[index]), review_digest
        ):
            raise RuntimeToolConflict(
                "Legacy configuration changed; review again before importing"
            )
        reviewed = self._review(items)[index]
        if reviewed["status"] == "already_saved":
            raise RuntimeToolConflict(
                "Connection name is already saved; existing connection unchanged"
            )
        if reviewed["status"] != "ready":
            raise ValueError("Legacy connection is not eligible for import")
        config, token = self.candidate(items[index])
        return self.manager.store.create(config, actor, token, event="legacy_imported")
