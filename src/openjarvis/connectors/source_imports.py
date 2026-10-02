"""Explicit, recoverable legacy imports. No provider requests or secret exports."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass

from cryptography.fernet import InvalidToken

from openjarvis.connectors.source_adapters import get_adapter
from openjarvis.connectors.source_credentials import _secret
from openjarvis.connectors.source_store import SourceConflict
from openjarvis.connectors.token_vault import TokenVault


@dataclass(frozen=True)
class LegacyImport:
    identity: str
    display_name: str
    adapter_id: str
    filename: str
    origin: str
    token_field: str = "token"


# Trusted definitions only: clients cannot supply files, origins or token fields.
_IMPORTS = {
    "notion": LegacyImport(
        "notion", "Notion", "notion_pages", "notion.json", "https://api.notion.com"
    ),
}


class SourceImports:
    def __init__(self, manager):
        self.manager = manager

    def _definition(self, identity):
        try:
            return _IMPORTS[identity]
        except KeyError:
            raise ValueError("Unsupported legacy integration import") from None

    def _vault(self, definition):
        return TokenVault(
            self.manager.store.path.parent / "connectors" / definition.filename
        )

    @staticmethod
    def _fingerprint(values):
        return hashlib.sha256(
            json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _mapping(self, identity):
        with self.manager.store.connection() as conn:
            row = conn.execute(
                "SELECT * FROM source_imports WHERE name=?", (identity,)
            ).fetchone()
        return dict(row) if row else None

    def _completed(self, mapping):
        if not mapping or not mapping["completed"]:
            return None
        try:
            source = self.manager.store.get(mapping["source_id"])
        except KeyError:
            raise SourceConflict(
                "Imported source was removed; create a new connection explicitly"
            ) from None
        return source

    def list(self):
        result = []
        for identity, definition in _IMPORTS.items():
            mapping = self._mapping(identity)
            state, source_id = "unavailable", None
            if mapping and mapping["completed"]:
                source_id = mapping["source_id"]
                try:
                    self._completed(mapping)
                    state = "imported"
                except SourceConflict:
                    state = "removed"
            else:
                try:
                    with self._vault(definition).inspect() as values:
                        if values and _secret(values.get(definition.token_field)):
                            state = "available"
                except (ValueError, OSError):
                    pass
            result.append(
                {
                    "import_id": identity,
                    "display_name": definition.display_name,
                    "adapter_id": definition.adapter_id,
                    "state": state,
                    "source_id": source_id,
                }
            )
        return result

    def preview(self, identity, name, *, actor="system"):
        definition = self._definition(identity)
        name = self.manager._name(name)
        existing = self._completed(self._mapping(identity))
        adapter = get_adapter(definition.adapter_id)
        defaults = adapter.validate_config({"credential_id": str(uuid.uuid4())})
        defaults.pop("credential_id")
        if existing:
            raise SourceConflict("This integration has already been imported")
        try:
            with self._vault(definition).inspect() as values:
                if not values:
                    raise ValueError("Missing legacy connection")
                _secret(values.get(definition.token_field))
                # The secret-derived digest is encrypted inside the ticket.
                # The public preview contains no credential value or fingerprint.
                ticket = (
                    self.manager.credentials._cipher()
                    .encrypt(
                        json.dumps(
                            {
                                "version": 1,
                                "import_id": identity,
                                "name": name,
                                "actor": actor,
                                "definition": asdict(definition),
                                "adapter_version": adapter.config_version,
                                "config": defaults,
                                "fingerprint": self._fingerprint(values),
                            }
                        ).encode()
                    )
                    .decode()
                )
        except (ValueError, OSError):
            raise ValueError(
                "Legacy connection is unavailable; check its vault and token"
            ) from None
        return {
            "import_id": identity,
            "adapter_id": definition.adapter_id,
            "name": name,
            "credential_origin": definition.origin,
            "config_version": adapter.config_version,
            "config": defaults,
            "settings": [
                {"label": field["label"], "value": defaults[field["name"]]}
                for field in adapter.fields
                if field["name"] in defaults
            ],
            "index_reset": False,
            "fresh_index": True,
            "legacy_connection_kept": True,
            "expires_in_seconds": 600,
            "plan_token": ticket,
        }

    def _reserve(self, identity):
        with self.manager.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO source_imports VALUES (?,?,?,0)",
                (identity, str(uuid.uuid4()), str(uuid.uuid4())),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM source_imports WHERE name=?", (identity,)
                ).fetchone()
            )

    def apply(self, identity, plan_token, *, actor="system"):
        definition = self._definition(identity)
        try:
            plan = json.loads(
                self.manager.credentials._cipher().decrypt(
                    plan_token.encode(),
                    ttl=600,
                )
            )
            if (
                plan["version"] != 1
                or plan["import_id"] != identity
                or plan["actor"] != actor
                or plan["definition"] != asdict(definition)
                or plan["adapter_version"]
                != get_adapter(definition.adapter_id).config_version
            ):
                raise ValueError
            name = self.manager._name(plan["name"])
        except (InvalidToken, ValueError, TypeError, KeyError, AttributeError, OSError):
            raise ValueError(
                "Import preview is invalid or expired; preview again"
            ) from None
        lock_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "source-import:" + identity))
        with self.manager.credentials._lock(lock_id, exclusive=True):
            existing = self._completed(self._mapping(identity))
            if existing:
                return existing
            vault = self._vault(definition)
            try:
                # Also upgrade a plaintext legacy file to its protected reference.
                vault.load()
                with vault.inspect() as values:
                    if not values or self._fingerprint(values) != plan["fingerprint"]:
                        raise SourceConflict("Legacy connection changed; preview again")
                    secret = _secret(values.get(definition.token_field))
                    mapping = self._reserve(identity)
                    adapter = get_adapter(definition.adapter_id)
                    config = adapter.validate_config(
                        {
                            **plan["config"],
                            "credential_id": mapping["credential_id"],
                        }
                    )
                    # Stable reserved IDs make a crash after credential commit
                    # recoverable without orphaning additional credential copies.
                    with self.manager.credentials.imported_bearer(
                        mapping["credential_id"],
                        name,
                        definition.origin,
                        secret,
                    ):
                        with self.manager.store.connection() as conn:
                            conn.execute("BEGIN IMMEDIATE")
                            self.manager.store._insert_source(
                                conn,
                                mapping["source_id"],
                                definition.adapter_id,
                                name,
                                config,
                                adapter.config_version,
                                actor,
                                action="imported",
                            )
                            conn.execute(
                                "UPDATE source_imports SET completed=1 WHERE name=?",
                                (identity,),
                            )
            except SourceConflict:
                raise
            except (ValueError, OSError, sqlite3.Error):
                raise ValueError(
                    "Legacy import failed; check the vault and retry"
                ) from None
            return self.manager.store.get(mapping["source_id"])
