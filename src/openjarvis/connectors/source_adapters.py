"""Versioned adapter definitions, separate from saved connection instances.

Only non-secret configuration is supported by this first adapter contract.
Credential-bearing adapters must add protected credential references before use.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from openjarvis.connectors._stubs import BaseConnector
from openjarvis.connectors.local_files import LocalFilesConnector


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

    def metadata(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "display_name": self.display_name,
            "description": self.description,
            "config_version": self.config_version,
            "fields": self.fields,
            "required_capabilities": self.required_capabilities,
            "operations": ["test", "sync", "remove"],
            "full_snapshot": self.full_snapshot,
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
