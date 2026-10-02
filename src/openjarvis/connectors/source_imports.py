"""Explicit, recoverable legacy imports. No provider requests or secret exports."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass

from cryptography.fernet import InvalidToken

from openjarvis.connectors.instance_sources import ACCOUNT_READERS, token_path
from openjarvis.connectors.oauth import (
    GOOGLE_ALL_SCOPES,
    connector_scopes,
    get_provider_for_connector,
)
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
    storage: str = "bearer"
    fallback_filename: str = ""


# Trusted definitions only: clients cannot supply files, origins or token fields.
_IMPORTS = {
    "notion": LegacyImport(
        "notion", "Notion", "notion_pages", "notion.json", "https://api.notion.com"
    ),
}


# Primary names match the legacy readers. Google falls back only when the
# product-specific file is absent, exactly as the legacy credential resolver.
_ACCOUNT_ORIGINS = {
    "gmail": "https://www.googleapis.com",
    "gdrive": "https://www.googleapis.com",
    "gcalendar": "https://www.googleapis.com",
    "gcontacts": "https://people.googleapis.com",
    "google_tasks": "https://tasks.googleapis.com",
    "spotify": "https://api.spotify.com",
    "strava": "https://www.strava.com",
    "slack": "https://slack.com",
    "dropbox": "https://api.dropboxapi.com",
    "granola": "https://public-api.granola.ai",
    "oura": "https://api.ouraring.com",
    "github_notifications": "https://api.github.com",
    "weather": "https://api.openweathermap.org",
}
for _service, (_, _, _, _auth) in ACCOUNT_READERS.items():
    _provider = get_provider_for_connector(_service)
    _filename = (
        "github.json" if _service == "github_notifications" else f"{_service}.json"
    )
    _fallback = (
        "google.json"
        if _provider and _provider.name == "google"
        else "github_notifications.json"
        if _service == "github_notifications"
        else ""
    )
    _IMPORTS[_service] = LegacyImport(
        _service,
        _service.replace("_", " ").title(),
        f"{_service}_account",
        _filename,
        _ACCOUNT_ORIGINS[_service],
        "access_token"
        if _auth == "oauth"
        else "api_key"
        if _service == "weather"
        else "token",
        "bundle",
        _fallback,
    )


def _account_payload(definition, values):
    """Normalize trusted reader fields only; never copy URLs or arbitrary keys."""
    adapter = get_adapter(definition.adapter_id)
    service = adapter.connection_service
    if not service:
        raise ValueError("Missing account adapter")
    config = adapter.validate_config(
        {"location": values.get("location")} if service == "weather" else {}
    )
    if adapter.connection_auth == "token":
        token = _secret(values.get(definition.token_field))
        if service == "slack" and not token.startswith("xoxp-"):
            raise ValueError("Slack requires a user token")
        if service == "weather" and token in config["location"]:
            raise ValueError("Location contains protected credential material")
        return config, {definition.token_field: token}

    # Old Google readers accept a pasted token under "token". Normalize it to
    # the named-account key; a minimal access-only grant remains importable.
    access = values.get("access_token", values.get("token"))
    bundle = {"access_token": _secret(access)}
    for key in ("refresh_token", "client_id", "client_secret"):
        if values.get(key) not in (None, ""):
            bundle[key] = _secret(values[key])
    if bool(bundle.get("client_id")) != bool(bundle.get("client_secret")):
        raise ValueError("Incomplete application registration")
    if bundle.get("refresh_token") and not bundle.get("client_id"):
        raise ValueError("Refresh requires an application registration")
    for key in ("expires_at", "expires_in"):
        if key in values:
            value = values[key]
            if type(value) not in (int, float) or (
                type(value) is float and not math.isfinite(value)
            ):
                raise ValueError("Invalid token expiry")
            if key == "expires_in" and not 0 < value <= 31536000:
                raise ValueError("Invalid token expiry")
            if key == "expires_at" and not 0 <= value <= 253402300799:
                raise ValueError("Invalid token expiry")
            bundle[key] = value
    if values.get("token_type") is not None:
        if values["token_type"] != "Bearer":
            raise ValueError("Unsupported token type")
        bundle["token_type"] = "Bearer"
    provider = get_provider_for_connector(service)
    if provider is None:
        raise ValueError("Missing OAuth provider")
    allowed = set(provider.scopes)
    if provider.name == "google":
        allowed.update(GOOGLE_ALL_SCOPES)
        allowed.update(
            {
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/userinfo.profile",
            }
        )
        for product in provider.connector_ids:
            allowed.update(connector_scopes(provider, product))
    scopes = values.get("requested_scopes")
    if scopes is None and values.get("scope") is not None:
        if not isinstance(values["scope"], str):
            raise ValueError("Invalid grant metadata")
        scopes = values["scope"].split()
    if scopes is not None:
        if (
            not isinstance(scopes, list)
            or not scopes
            or len(scopes) > 30
            or any(
                not isinstance(scope, str) or scope not in allowed for scope in scopes
            )
        ):
            raise ValueError("Invalid grant metadata")
        needed = set(connector_scopes(provider, service))
        # Preserve older Google write grants that contain the requested read
        # access. Import never upgrades, narrows or claims to verify a grant.
        equivalents = {
            "https://www.googleapis.com/auth/calendar.readonly": "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/gmail.readonly": "https://www.googleapis.com/auth/gmail.modify",
        }
        if any(
            scope not in scopes and equivalents.get(scope) not in scopes
            for scope in needed
        ):
            raise ValueError("Legacy grant does not cover this product")
        preserved = set(scopes)
        if provider.name == "google":
            for short, full in (
                ("email", "https://www.googleapis.com/auth/userinfo.email"),
                ("profile", "https://www.googleapis.com/auth/userinfo.profile"),
            ):
                if short in preserved or full in preserved:
                    preserved.update((short, full))
        bundle["requested_scopes"] = sorted(preserved)
    else:
        # This is a refresh response allowlist, not a claim about actual grants.
        # Missing grant metadata cannot prove least privilege; the web review
        # explicitly recommends new consent for all imported OAuth grants.
        bundle["requested_scopes"] = sorted(allowed)
    return config, bundle


class SourceImports:
    def __init__(self, manager):
        self.manager = manager

    def _definition(self, identity):
        try:
            return _IMPORTS[identity]
        except KeyError:
            raise ValueError("Unsupported legacy integration import") from None

    def _vault(self, definition):
        directory = self.manager.store.path.parent / "connectors"
        path = directory / definition.filename
        if not path.exists() and not path.is_symlink() and definition.fallback_filename:
            path = directory / definition.fallback_filename
        return TokenVault(path)

    def _prepared(self, definition, values):
        if not values:
            raise ValueError("Missing legacy connection")
        if definition.storage == "bundle":
            return _account_payload(definition, values)
        _secret(values.get(definition.token_field))
        adapter = get_adapter(definition.adapter_id)
        config = adapter.validate_config({"credential_id": str(uuid.uuid4())})
        config.pop("credential_id")
        return config, None

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
                        self._prepared(definition, values)
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
        if existing:
            raise SourceConflict("This integration has already been imported")
        try:
            vault = self._vault(definition)
            with vault.inspect() as values:
                defaults, bundle = self._prepared(definition, values)
                binding = vault.binding
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
                                "legacy_binding": binding,
                            }
                        ).encode()
                    )
                    .decode()
                )
        except (ValueError, OSError):
            raise ValueError(
                "Legacy connection is unavailable; check its vault and token"
            ) from None
        refresh_available = bool(bundle and bundle.get("refresh_token"))
        if bundle:
            bundle.clear()
        return {
            "import_id": identity,
            "adapter_id": definition.adapter_id,
            "name": name,
            "credential_origin": definition.origin,
            "credential_storage": definition.storage,
            "oauth_grant_preserved": adapter.connection_auth == "oauth",
            "refresh_available": refresh_available,
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

    def _reserve(self, identity, definition):
        with self.manager.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source_id = str(uuid.uuid4())
            credential_id = (
                str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL, f"connectors/instance-{source_id}.json"
                    )
                )
                if definition.storage == "bundle"
                else str(uuid.uuid4())
            )
            conn.execute(
                "INSERT OR IGNORE INTO source_imports VALUES (?,?,?,0)",
                (identity, source_id, credential_id),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM source_imports WHERE name=?", (identity,)
                ).fetchone()
            )

    @contextmanager
    def _imported_credential(self, definition, mapping, name, values, bundle):
        if definition.storage == "bundle":
            destination = TokenVault(
                token_path(self.manager.store.path.parent, mapping["source_id"])
            )
            if destination.identity != mapping["credential_id"]:
                raise SourceConflict("Reserved credential binding changed")
            with destination.imported_bundle(bundle):
                yield
        else:
            with self.manager.credentials.imported_bearer(
                mapping["credential_id"],
                name,
                definition.origin,
                _secret(values.get(definition.token_field)),
            ):
                yield

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
                if vault.binding != plan.get("legacy_binding"):
                    raise SourceConflict(
                        "Legacy credential selection changed; preview again"
                    )
                vault.load()
                with vault.inspect() as values:
                    if (
                        self._vault(definition).binding != vault.binding
                        or not values
                        or self._fingerprint(values) != plan["fingerprint"]
                    ):
                        raise SourceConflict("Legacy connection changed; preview again")
                    defaults, bundle = self._prepared(definition, values)
                    if defaults != plan["config"]:
                        raise SourceConflict(
                            "Legacy configuration changed; preview again"
                        )
                    mapping = self._reserve(identity, definition)
                    adapter = get_adapter(definition.adapter_id)
                    config = adapter.validate_config(
                        {**defaults, "credential_id": mapping["credential_id"]}
                        if definition.storage == "bearer"
                        else defaults
                    )
                    # Hold the reserved credential lock through source commit;
                    # a retry may reuse but never overwrite the same identity.
                    with self._imported_credential(
                        definition, mapping, name, values, bundle
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
