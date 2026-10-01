"""Versioned adapter definitions, separate from saved connection instances.

Secrets live in the server vault; adapter configuration carries references only.
"""

from __future__ import annotations

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

    def metadata(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "display_name": self.display_name,
            "description": self.description,
            "config_version": self.config_version,
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


_ADAPTERS: dict[str, SourceAdapter] = {}


def register_adapter(adapter: SourceAdapter) -> None:
    """Register trusted server code; discovery never grants its capabilities."""
    if adapter.adapter_id in _ADAPTERS:
        raise ValueError(f"Source adapter already registered: {adapter.adapter_id}")
    if any(field.get("secret") for field in adapter.fields):
        raise ValueError("Secret fields require a credential-reference implementation")
    if not adapter.required_capabilities:
        raise ValueError("Source adapters must declare capability requirements")
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
                "label": "Maximum records",
                "type": "number",
                "default_value": 200,
                "min": 1,
                "max": 1000,
                "visible_when": _RECORDS,
            },
            {
                "name": "complete_snapshot",
                "label": "This response is a complete snapshot",
                "type": "checkbox",
                "required": False,
                "default_value": False,
                "description": (
                    "Remove missing records only when the response "
                    "contains the complete collection."
                ),
                "visible_when": _RECORDS,
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
