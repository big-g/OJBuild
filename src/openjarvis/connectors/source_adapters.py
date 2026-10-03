"""Versioned adapter definitions, separate from saved connection instances.

Secrets live in the server vault; adapter configuration carries references only.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from openjarvis.connectors._stubs import BaseConnector
from openjarvis.connectors.local_files import LocalFilesConnector
from openjarvis.connectors.web_sources import (
    JsonAPIConnector,
    WebPageConnector,
    probe_public_source,
    validate_json_config,
    validate_web_config,
)


@dataclass(frozen=True)
class ConfigMigration:
    """Trusted, local N -> N+1 transformation; reset indexing by default."""

    from_version: int
    transform: Callable[[dict[str, Any]], dict[str, Any]]
    preserves_index: bool = False


@dataclass(frozen=True)
class SourceAdapter:
    adapter_id: str
    display_name: str
    description: str
    fields: tuple[dict[str, Any], ...]
    validate: Callable[[dict[str, Any]], dict[str, Any]]
    factory: Callable[[dict[str, Any]], BaseConnector]
    required_capabilities: tuple[str, ...]
    config_version: int = 1
    full_snapshot: bool = False
    snapshot_config_field: str | None = None
    probe: Callable[[BaseConnector], dict[str, Any]] | None = None
    credential_kinds: tuple[str, ...] = ()
    bind_credential: Callable[[BaseConnector, dict], None] | None = None
    migrations: tuple[ConfigMigration, ...] = ()
    instance_factory: Callable | None = None
    connection_service: str | None = None
    connection_auth: str | None = None
    credential_url: Callable[[dict[str, Any]], str] | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "connection_auth": self.connection_auth,
            "display_name": self.display_name,
            "description": self.description,
            "config_version": self.config_version,
            "migration_from_versions": [step.from_version for step in self.migrations],
            "fields": self.fields,
            "credential_kinds": self.credential_kinds,
            "required_capabilities": self.required_capabilities,
            "operations": ["test", "sync", "remove"],
            "full_snapshot": self.full_snapshot,
            "snapshot_config_field": self.snapshot_config_field,
        }

    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        allowed = {field["name"] for field in self.fields}
        if set(config) - allowed:
            raise ValueError("Configuration contains unsupported fields")
        return self.validate(config)

    def migrate_config(self, config: dict[str, Any], version: int):
        if type(version) is not int or not 1 <= version < self.config_version:
            raise ValueError("No upgrade is available for this configuration version")
        steps = {step.from_version: step for step in self.migrations}
        result, reset = deepcopy(config), False
        for current in range(version, self.config_version):
            step = steps.get(current)
            if step is None:
                raise ValueError("Adapter has no complete migration path")
            try:
                result = step.transform(deepcopy(result))
                if not isinstance(result, dict):
                    raise ValueError
            except Exception:
                raise ValueError("Adapter configuration migration failed") from None
            reset = reset or not step.preserves_index
        try:
            result = self.validate_config(result)
        except Exception:
            raise ValueError("Migrated configuration is invalid") from None
        if result.get("credential_id") != config.get("credential_id"):
            raise ValueError("Migration cannot change protected credential references")
        return result, reset


_ADAPTERS: dict[str, SourceAdapter] = {}


def register_adapter(adapter: SourceAdapter) -> None:
    """Register trusted server code; discovery never grants its capabilities."""
    if adapter.adapter_id in _ADAPTERS:
        raise ValueError(f"Source adapter already registered: {adapter.adapter_id}")
    if any(field.get("secret") for field in adapter.fields):
        raise ValueError("Secret fields require a credential-reference implementation")
    if not adapter.required_capabilities:
        raise ValueError("Source adapters must declare capability requirements")
    if type(adapter.config_version) is not int or adapter.config_version < 1:
        raise ValueError("Adapter configuration version must be a positive integer")
    versions = [step.from_version for step in adapter.migrations]
    if len(set(versions)) != len(versions) or any(
        type(step.from_version) is not int
        or not 1 <= step.from_version < adapter.config_version
        or not callable(step.transform)
        or type(step.preserves_index) is not bool
        for step in adapter.migrations
    ):
        raise ValueError("Invalid adapter configuration migration steps")
    _ADAPTERS[adapter.adapter_id] = adapter


def get_adapter(adapter_id: str) -> SourceAdapter:
    try:
        return _ADAPTERS[adapter_id]
    except KeyError as exc:
        raise ValueError(f"Unknown source adapter: {adapter_id}") from exc


def list_adapters() -> list[dict[str, Any]]:
    return [_ADAPTERS[key].metadata() for key in sorted(_ADAPTERS)]


def is_source_adapter(adapter_id: str) -> bool:
    return adapter_id in _ADAPTERS


def _validate_local_files(config: dict[str, Any]) -> dict[str, Any]:
    path = config.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("A directory path on the OpenJarvis server is required")
    root = Path(path).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Local Files requires a directory on the server")
    return {"path": str(root)}


register_adapter(
    SourceAdapter(
        adapter_id="local_files",
        display_name="Local Files",
        description="Text, Markdown, CSV/TSV, JSON, HTML and text-based PDFs.",
        fields=(
            {
                "name": "path",
                "label": "Server folder",
                "type": "text",
                "required": True,
                "placeholder": "/home/you/Documents",
            },
        ),
        validate=_validate_local_files,
        factory=lambda config: LocalFilesConnector(root_path=config["path"]),
        required_capabilities=LocalFilesConnector.capability_requirements(),
        full_snapshot=True,
    )
)

_URL_FIELD = {
    "name": "url",
    "label": "URL",
    "type": "text",
    "required": True,
    "placeholder": "https://example.org/resource",
}
_CREDENTIAL_FIELD = {
    "name": "credential_id",
    "label": "Credential",
    "type": "credential",
    "required": False,
    "description": "Optional protected credential for this HTTPS origin.",
    "credential_kinds": ["bearer", "api_key"],
}
_RECORDS = {"field": "mode", "equals": "records"}

register_adapter(
    SourceAdapter(
        adapter_id="web_page",
        display_name="Web Page",
        description="Index one HTML or text page. JavaScript is not executed.",
        fields=(_URL_FIELD, _CREDENTIAL_FIELD),
        validate=validate_web_config,
        factory=lambda config: WebPageConnector(config=config),
        required_capabilities=WebPageConnector.capability_requirements(),
        full_snapshot=True,
        probe=probe_public_source,
        credential_kinds=("bearer", "api_key"),
        bind_credential=lambda reader, material: reader.bind_credential(material),
    )
)

register_adapter(
    SourceAdapter(
        adapter_id="json_api",
        display_name="JSON API",
        description=(
            "Read a JSON GET response as one document or map an array to records."
        ),
        fields=(
            _URL_FIELD,
            _CREDENTIAL_FIELD,
            {
                "name": "mode",
                "value_updates": {
                    "document": {
                        "pagination": "none",
                        "sync_mode": "snapshot",
                        "complete_snapshot": False,
                        "deleted_ids_pointer": "",
                    }
                },
                "label": "Index as",
                "type": "select",
                "default_value": "document",
                "options": [
                    {"value": "document", "label": "Whole JSON document"},
                    {"value": "records", "label": "Individual records"},
                ],
            },
            {
                "name": "records_pointer",
                "label": "Records array pointer",
                "type": "text",
                "placeholder": "/data/items (empty for root array)",
                "required": False,
                "visible_when": _RECORDS,
            },
            {
                "name": "id_pointer",
                "label": "Record ID pointer",
                "type": "text",
                "default_value": "/id",
                "visible_when": _RECORDS,
            },
            {
                "name": "title_pointer",
                "label": "Title pointer",
                "type": "text",
                "placeholder": "/title (empty to use record ID)",
                "required": False,
                "visible_when": _RECORDS,
            },
            {
                "name": "content_pointer",
                "label": "Content pointer",
                "type": "text",
                "placeholder": "/body (empty to index the whole record)",
                "required": False,
                "visible_when": _RECORDS,
            },
            {
                "name": "max_records",
                "label": "Maximum records and deletion IDs per sync",
                "type": "number",
                "default_value": 200,
                "min": 1,
                "max": 1000,
                "visible_when": _RECORDS,
            },
            {
                "name": "pagination",
                "label": "Pagination",
                "type": "select",
                "default_value": "none",
                "visible_when": _RECORDS,
                "options": [
                    {"value": "none", "label": "Single response"},
                    {"value": "next_url", "label": "Next-page URL in JSON"},
                    {"value": "cursor", "label": "Page cursor in JSON"},
                ],
            },
            {
                "name": "next_pointer",
                "required": True,
                "label": "Next-page value pointer",
                "type": "text",
                "placeholder": "/next",
                "visible_when": [
                    _RECORDS,
                    {"field": "pagination", "one_of": ["next_url", "cursor"]},
                ],
                "description": (
                    "Required on every page; null or empty string marks the final page."
                ),
            },
            {
                "name": "cursor_parameter",
                "label": "Page cursor query parameter",
                "type": "text",
                "default_value": "cursor",
                "visible_when": [_RECORDS, {"field": "pagination", "equals": "cursor"}],
            },
            {
                "name": "max_pages",
                "label": "Maximum pages",
                "type": "number",
                "default_value": 10,
                "min": 1,
                "max": 50,
                "visible_when": [
                    _RECORDS,
                    {"field": "pagination", "one_of": ["next_url", "cursor"]},
                ],
                "description": (
                    "Exceeding a limit fails the scan without "
                    "applying missing-record cleanup."
                ),
            },
            {
                "name": "sync_mode",
                "value_updates": {
                    "incremental": {"complete_snapshot": False},
                    "snapshot": {"deleted_ids_pointer": ""},
                },
                "label": "Sync contract",
                "type": "select",
                "default_value": "snapshot",
                "visible_when": _RECORDS,
                "options": [
                    {"value": "snapshot", "label": "Snapshot / append records"},
                    {
                        "value": "incremental",
                        "label": "Incremental changes with durable token",
                    },
                ],
            },
            {
                "name": "sync_token_pointer",
                "required": True,
                "label": "Final-page sync token pointer",
                "type": "text",
                "placeholder": "/sync_token",
                "visible_when": [
                    _RECORDS,
                    {"field": "sync_mode", "equals": "incremental"},
                ],
                "description": (
                    "Server-issued non-secret token. Saved only after "
                    "successful ingestion and cleanup."
                ),
            },
            {
                "name": "sync_token_parameter",
                "label": "Sync token query parameter",
                "type": "text",
                "default_value": "since",
                "visible_when": [
                    _RECORDS,
                    {"field": "sync_mode", "equals": "incremental"},
                ],
            },
            {
                "name": "deleted_ids_pointer",
                "label": "Deleted record IDs pointer",
                "type": "text",
                "placeholder": "/deleted_ids",
                "visible_when": [
                    _RECORDS,
                    {"field": "sync_mode", "equals": "incremental"},
                ],
                "description": (
                    "Optional array of explicitly deleted IDs. Missing "
                    "records are retained in incremental mode."
                ),
            },
            {
                "name": "complete_snapshot",
                "label": "This response is a complete snapshot",
                "type": "checkbox",
                "required": False,
                "default_value": False,
                "description": (
                    "Remove missing records only when the response "
                    "contains the complete collection across all pages."
                ),
                "visible_when": [
                    _RECORDS,
                    {"field": "sync_mode", "equals": "snapshot"},
                ],
            },
        ),
        validate=validate_json_config,
        factory=lambda config: JsonAPIConnector(config=config),
        required_capabilities=JsonAPIConnector.capability_requirements(),
        full_snapshot=True,
        snapshot_config_field="complete_snapshot",
        probe=probe_public_source,
        credential_kinds=("bearer", "api_key"),
        bind_credential=lambda reader, material: reader.bind_credential(material),
    )
)


# Provider adapters keep their trusted endpoints outside saved configuration.
from openjarvis.connectors.notion_sources import (  # noqa: E402
    NOTION_ORIGIN,
    NotionSource,
    validate_notion_config,
)

register_adapter(
    SourceAdapter(
        adapter_id="notion_pages",
        display_name="Notion pages",
        description=(
            "Read pages shared with a Notion integration. Add a protected bearer "
            "credential for https://api.notion.com and select it here."
        ),
        fields=(
            {
                "name": "credential_id",
                "label": "Notion integration credential",
                "type": "credential",
                "required": True,
                "credential_kinds": ["bearer"],
                "credential_origin": NOTION_ORIGIN,
                "description": "Share the desired pages with this Notion integration.",
            },
            {
                "name": "query",
                "label": "Title filter",
                "type": "text",
                "description": "Optional title search; empty means accessible pages.",
            },
            {
                "name": "max_pages",
                "label": "Page limit",
                "type": "number",
                "default_value": 100,
                "min": 1,
                "max": 500,
            },
            {
                "name": "max_requests",
                "label": "Request limit",
                "type": "number",
                "default_value": 300,
                "min": 1,
                "max": 1000,
            },
            {
                "name": "max_blocks",
                "label": "Block limit",
                "type": "number",
                "default_value": 5000,
                "min": 1,
                "max": 20000,
                "description": "Scan limits fail without advancing sync state.",
            },
        ),
        validate=validate_notion_config,
        factory=lambda config: NotionSource(config=config),
        required_capabilities=NotionSource.capability_requirements(),
        credential_kinds=("bearer",),
        credential_url=lambda config: NOTION_ORIGIN,
        bind_credential=lambda reader, material: reader.bind_credential(material),
        probe=lambda reader: reader.probe(),
    )
)


from openjarvis.connectors.instance_sources import (  # noqa: E402
    register_instance_adapters,
)

register_instance_adapters(register_adapter, SourceAdapter)

from openjarvis.connectors.imap_sources import register_imap_adapter  # noqa: E402

register_imap_adapter(register_adapter, SourceAdapter)
