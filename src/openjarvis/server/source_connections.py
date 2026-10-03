"""Authenticated, instance-scoped credentials and shared OAuth lifecycle."""

from datetime import datetime, timezone
from functools import wraps

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from openjarvis.connectors.instance_sources import (
    account_record,
    disconnect_instance,
    token_path,
)
from openjarvis.connectors.oauth import load_tokens, save_tokens
from openjarvis.connectors.oauth_state import OAuthStateStore
from openjarvis.connectors.source_audit import append_event
from openjarvis.connectors.source_credentials import _secret
from openjarvis.server.oauth_broker import install_oauth_routes


class AccountToken(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    token: SecretStr


class AccountClient(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    client_id: SecretStr
    client_secret: SecretStr


class AccountPassword(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    username: SecretStr
    password: SecretStr


class AccountDisconnect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)


def install_source_connections(router, manager, invoke):
    directory = manager.store.path.parent

    def audit(identity, action, actor=None):
        record = manager.store.get(identity)
        with manager.store.connection() as conn:
            conn.execute(
                "UPDATE sources SET revision=revision+1,updated_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), identity),
            )
            record["revision"] += 1
            append_event(
                conn,
                record,
                action,
                index_reset=True,
                fields=("account_authorization",),
                actor=f"user:{actor}" if actor is not None else "server_access",
            )

    def serialized(fn):
        @wraps(fn)
        def call(connector_id: str, *args, **kwargs):
            def run():
                with manager._locked(connector_id):
                    return fn(connector_id, *args, **kwargs)

            return invoke(run)

        return call

    class Context:
        def directory(self):
            return directory

        def service(self, identity):
            return invoke(account_record, manager, identity)[1].connection_service

        def ensure(self, identity):
            invoke(account_record, manager, identity, require_enabled=True)

        def binding(self, identity):
            record, adapter = invoke(account_record, manager, identity)
            values = load_tokens(str(token_path(directory, identity)))
            return [
                record["revision"],
                adapter.adapter_id,
                adapter.config_version,
                getattr(values, "revision", 0),
            ]

        def token_path(self, identity, provider):
            return token_path(directory, identity)

        def client_credentials(self, identity, provider):
            from openjarvis.connectors.oauth import get_client_credentials

            values = load_tokens(str(token_path(directory, identity))) or {}
            if values.get("client_id") and values.get("client_secret"):
                return values["client_id"], values["client_secret"]
            return get_client_credentials(provider)

        def before_save(self, identity):
            manager._reset_and_purge(manager.store.get(identity))

        def connected(self, identity, actor=None):
            audit(identity, "account_authorized", actor)

    install_oauth_routes(router, Context(), serialized)

    @router.get("/{identity}/connection")
    def connection(identity: str):
        record, adapter = invoke(account_record, manager, identity)
        values = load_tokens(str(token_path(directory, identity))) or {}
        key = (
            "password"
            if adapter.connection_auth == "password"
            else "access_token"
            if adapter.connection_auth == "oauth"
            else ("api_key" if adapter.connection_service == "weather" else "token")
        )
        return {
            "auth_type": adapter.connection_auth,
            "connected": bool(values.get(key)),
            "client_configured": bool(
                values.get("client_id") and values.get("client_secret")
            ),
        }

    @router.put("/{identity}/connection/password")
    def set_password(identity: str, req: AccountPassword, request: Request):
        with manager._locked(identity):
            record, adapter = invoke(account_record, manager, identity, req.revision)
            if adapter.connection_auth != "password":
                raise HTTPException(
                    400, "This source does not use password authorization"
                )
            username = invoke(_secret, req.username.get_secret_value())
            password = req.password.get_secret_value()
            if (
                not password
                or len(password) > 4096
                or any(ord(c) < 32 or ord(c) > 126 for c in password)
            ):
                raise HTTPException(400, "Invalid account password")
            manager._reset_and_purge(record)
            OAuthStateStore(directory).cancel(identity)
            invoke(
                save_tokens,
                str(token_path(directory, identity)),
                {"username": username, "password": password},
            )
            audit(
                identity,
                "account_password_replaced",
                getattr(request.state, "auth_user_id", None),
            )
        return {"connected": True}

    @router.put("/{identity}/connection/token")
    def set_token(identity: str, req: AccountToken, request: Request):
        with manager._locked(identity):
            record, adapter = invoke(account_record, manager, identity, req.revision)
            if adapter.connection_auth != "token":
                raise HTTPException(400, "This source requires OAuth authorization")
            token = invoke(_secret, req.token.get_secret_value())
            if adapter.connection_service == "slack" and not token.startswith("xoxp-"):
                raise HTTPException(400, "Slack requires a user token")
            manager._reset_and_purge(record)
            OAuthStateStore(directory).cancel(identity)
            key = "api_key" if adapter.connection_service == "weather" else "token"
            invoke(save_tokens, str(token_path(directory, identity)), {key: token})
            audit(
                identity,
                "account_token_replaced",
                getattr(request.state, "auth_user_id", None),
            )
        return {"connected": True}

    @router.put("/{identity}/connection/client")
    def set_client(identity: str, req: AccountClient, request: Request):
        with manager._locked(identity):
            record, adapter = invoke(account_record, manager, identity, req.revision)
            if adapter.connection_auth != "oauth":
                raise HTTPException(400, "This source uses a token credential")
            client_id = invoke(_secret, req.client_id.get_secret_value())
            client_secret = invoke(_secret, req.client_secret.get_secret_value())
            manager._reset_and_purge(record)
            OAuthStateStore(directory).cancel(identity)
            invoke(
                save_tokens,
                str(token_path(directory, identity)),
                {"client_id": client_id, "client_secret": client_secret},
            )
            audit(
                identity,
                "account_client_replaced",
                getattr(request.state, "auth_user_id", None),
            )
        return {"client_configured": True, "connected": False}

    @router.post("/{identity}/connection/disconnect")
    def disconnect(identity: str, req: AccountDisconnect, request: Request):
        with manager._locked(identity):
            record, adapter = invoke(account_record, manager, identity, req.revision)
            manager._reset_and_purge(record)
            invoke(disconnect_instance, manager, identity)
            audit(
                identity,
                "account_disconnected",
                getattr(request.state, "auth_user_id", None),
            )
        return {"connected": False}
