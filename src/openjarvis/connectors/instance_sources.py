"""Named instances of trusted account readers, with isolated vault bindings.

Source configuration contains no paths or secrets. Legacy connector endpoints
remain separate; instance credentials never fall back to a legacy account.
"""

from __future__ import annotations

import base64
import importlib
import time
import uuid
from pathlib import Path

import httpx

from openjarvis.connectors._stubs import BaseConnector
from openjarvis.connectors.oauth import (
    get_provider_for_connector,
    load_tokens,
    require_access_token,
    save_tokens,
)
from openjarvis.connectors.source_store import SourceConflict
from openjarvis.connectors.token_vault import TokenVault

# Trusted server code: module, class, constructor argument, authentication.
ACCOUNT_READERS = {
    "gmail": ("gmail", "GmailConnector", "credentials_path", "oauth"),
    "gdrive": ("gdrive", "GDriveConnector", "credentials_path", "oauth"),
    "gcalendar": ("gcalendar", "GCalendarConnector", "credentials_path", "oauth"),
    "gcontacts": ("gcontacts", "GContactsConnector", "credentials_path", "oauth"),
    "google_tasks": (
        "google_tasks",
        "GoogleTasksConnector",
        "credentials_path",
        "oauth",
    ),
    "spotify": ("spotify", "SpotifyConnector", "token_path", "oauth"),
    "strava": ("strava", "StravaConnector", "token_path", "oauth"),
    "slack": ("slack_connector", "SlackConnector", "credentials_path", "token"),
    "dropbox": ("dropbox", "DropboxConnector", "credentials_path", "token"),
    "granola": ("granola", "GranolaConnector", "credentials_path", "token"),
    "oura": ("oura", "OuraConnector", "token_path", "token"),
    "github_notifications": (
        "github_notifications",
        "GitHubNotificationsConnector",
        "token_path",
        "token",
    ),
    "weather": ("weather", "WeatherConnector", "token_path", "token"),
}


def token_path(directory, identity):
    return Path(directory) / "connectors" / f"instance-{uuid.UUID(identity)}.json"


def validate_config(service, config):
    from openjarvis.connectors.account_scans import (
        REMAINING_SERVICES,
        validate_scan_config,
    )

    if service in REMAINING_SERVICES:
        return validate_scan_config(service, config)
    if service == "slack":
        from openjarvis.connectors.slack_sources import validate_slack_config

        return validate_slack_config(config)
    if service in {"spotify", "strava"}:
        from openjarvis.connectors.activity_sources import validate_activity_config

        return validate_activity_config(config)
    if config:
        raise ValueError("This account adapter has no configuration fields")
    return {}


def refresh_instance(path, service):
    """Refresh only this bundle; vault revision checks reject stale writes."""
    values = load_tokens(str(path))
    if not values or not values.get("refresh_token"):
        return
    if float(values.get("expires_at", 0)) > time.time() + 60:
        return
    provider = get_provider_for_connector(service)
    if not provider:
        return
    data = {"grant_type": "refresh_token", "refresh_token": values["refresh_token"]}
    headers = {}
    if provider.token_auth == "basic":
        encoded = base64.b64encode(
            f"{values['client_id']}:{values['client_secret']}".encode()
        ).decode()
        headers["Authorization"] = f"Basic {encoded}"
    else:
        data.update(
            client_id=values["client_id"], client_secret=values["client_secret"]
        )
    try:
        response = httpx.post(
            provider.token_endpoint,
            data=data,
            headers=headers,
            timeout=30,
            follow_redirects=False,
            trust_env=False,
        )
        response.raise_for_status()
        if len(response.content) > 65536:
            raise ValueError
        payload = response.json()
        token = require_access_token(payload)
        granted = payload.get("scope")
        if granted is not None and (
            not isinstance(granted, str)
            or not set(granted.split()).issubset(values.get("requested_scopes", []))
        ):
            raise ValueError
        expiry = float(payload.get("expires_in", 3600))
        if not 0 < expiry <= 31536000:
            raise ValueError
        values.update(access_token=token, expires_at=time.time() + expiry)
        if payload.get("refresh_token"):
            values["refresh_token"] = payload["refresh_token"]
        save_tokens(str(path), values)
    except Exception:
        raise ValueError(
            "Account token refresh failed; authorize this source again"
        ) from None
    finally:
        values.clear()


class AccountSource(BaseConnector):
    def __init__(self, service, record, directory):
        self.service = service
        self.connector_id = service
        self.path = token_path(directory, record["id"])
        from openjarvis.connectors.account_scans import REMAINING_SERVICES

        if service in REMAINING_SERVICES:
            from openjarvis.connectors.google_sources import GoogleSource
            from openjarvis.connectors.provider_sources import ProviderSource

            reader_type = (
                GoogleSource
                if service
                in {"gmail", "gdrive", "gcalendar", "gcontacts", "google_tasks"}
                else ProviderSource
            )
            self.reader = reader_type(
                service=service, token_path=str(self.path), config=record["config"]
            )
            return
        module, cls, argument, _ = ACCOUNT_READERS[service]
        reader_type = getattr(
            importlib.import_module(f"openjarvis.connectors.{module}"), cls
        )
        self.reader = reader_type(**{argument: str(self.path)})
        if service == "slack":
            from openjarvis.connectors.slack_sources import SlackSource

            self.reader = SlackSource(
                credentials_path=str(self.path), config=record["config"]
            )
        if service in {"spotify", "strava"}:
            from openjarvis.connectors.activity_sources import ActivitySource

            self.reader = ActivitySource(
                service=service, token_path=str(self.path), config=record["config"]
            )

    def is_connected(self):
        values = load_tokens(str(self.path)) or {}
        key = (
            "access_token"
            if ACCOUNT_READERS[self.service][3] == "oauth"
            else ("api_key" if self.service == "weather" else "token")
        )
        return bool(values.get(key))

    def disconnect(self):
        TokenVault(self.path).delete()

    def sync_status(self):
        return self.reader.sync_status()

    def bind_sync_control(self, control):
        super().bind_sync_control(control)
        self.reader.bind_sync_control(control)

    def sync(self, *, since=None, cursor=None):
        if not self.is_connected():
            raise ValueError("Authorize this source before syncing")
        try:
            if ACCOUNT_READERS[self.service][3] == "oauth":
                refresh_instance(self.path, self.service)
            # A failed provider traversal must not yield a partial named scan.
            # Conservative staging limits guard unbounded legacy traversals.
            documents, size = [], 0
            for document in self.reader.sync(since=since, cursor=cursor):
                self.check_sync_cancelled()
                size += len(document.content.encode())
                if len(documents) >= 10000 or size > 32 * 1024 * 1024:
                    raise ValueError("Account scan exceeds limits")
                documents.append(document)
            self.check_sync_cancelled()
        except Exception:
            import sys

            from openjarvis.connectors.sync_control import (
                SyncCancelled,
                SyncLimitExceeded,
            )

            if isinstance(sys.exception(), (SyncCancelled, SyncLimitExceeded)):
                raise
            raise ValueError(
                "Account sync failed; check this source authorization"
            ) from None
        yield from documents


def register_instance_adapters(register, adapter_type):
    from openjarvis.connectors.account_scans import LIMITS, REMAINING_SERVICES
    from openjarvis.connectors.source_adapters import ConfigMigration

    for service, (_, _, _, auth) in ACCOUNT_READERS.items():
        display = service.replace("_", " ").title()
        fields = (
            (
                {
                    "name": "location",
                    "label": "Weather location",
                    "type": "text",
                    "required": True,
                },
            )
            if service == "weather"
            else ()
        )
        if service in {"spotify", "strava"}:
            from openjarvis.connectors.activity_sources import SCAN_LIMITS

            fields = tuple(
                {
                    "name": field,
                    "label": field.replace("_", " ").title(),
                    "type": "number",
                    "default_value": default,
                    "min": 10 if field == "timeout_seconds" else 1,
                    "max": maximum,
                }
                for field, (default, maximum) in SCAN_LIMITS.items()
            )
        if service == "slack":
            from openjarvis.connectors.slack_sources import SLACK_LIMITS

            fields = tuple(
                {
                    "name": field,
                    "label": field.replace("_", " ").title(),
                    "type": "number",
                    "default_value": default,
                    "min": 10 if field == "timeout_seconds" else 1,
                    "max": maximum,
                }
                for field, (default, maximum) in SLACK_LIMITS.items()
            )
        if service in REMAINING_SERVICES:
            fields += tuple(
                {
                    "name": field,
                    "label": field.replace("_", " ").title(),
                    "type": "number",
                    "default_value": default,
                    "min": 10 if field == "timeout_seconds" else 1,
                    "max": maximum,
                }
                for field, (default, maximum) in LIMITS.items()
            )
            if service == "oura":
                fields += (
                    {
                        "name": "lookback_days",
                        "label": "Lookback days",
                        "type": "number",
                        "default_value": 30,
                        "min": 1,
                        "max": 3650,
                    },
                )
        register(
            adapter_type(
                adapter_id=f"{service}_account",
                display_name=f"{display} account",
                description=(
                    "Save a named source, then authorize it. Credentials and sync "
                    "state belong to this connection."
                ),
                fields=fields,
                validate=lambda config, service=service: validate_config(
                    service, config
                ),
                factory=lambda config: None,
                instance_factory=lambda record, directory, service=service: (
                    AccountSource(service, record, directory)
                ),
                connection_service=service,
                connection_auth=auth,
                config_version=2 if service in REMAINING_SERVICES else 1,
                migrations=(ConfigMigration(1, lambda config: config),)
                if service in REMAINING_SERVICES
                else (),
                required_capabilities=(
                    f"connector:{service}:read",
                    "network:fetch",
                    "credential:use",
                ),
            )
        )


def disconnect_instance(manager, identity):
    from openjarvis.connectors.oauth_state import OAuthStateStore

    directory = manager.store.path.parent
    OAuthStateStore(directory).cancel(identity)
    TokenVault(token_path(directory, identity)).delete()


def account_record(manager, identity, revision=None, *, require_enabled=False):
    from openjarvis.connectors.source_adapters import get_adapter

    record = manager.store.get(identity)
    adapter = get_adapter(record["adapter_id"])
    if not adapter.connection_service:
        raise ValueError("Source does not use account authorization")
    if revision is not None and record["revision"] != revision:
        raise SourceConflict("Source changed; refresh before changing authorization")
    if require_enabled and not record["enabled"]:
        raise SourceConflict("Source is disabled")
    return record, adapter
