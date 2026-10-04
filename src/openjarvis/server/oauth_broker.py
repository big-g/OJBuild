"""Shared browser OAuth broker for legacy and named connection identities."""

import time

from fastapi import HTTPException, Request


def install_oauth_routes(router, context, serialized):
    def authorize_actor(connector_id, payload, request):
        check = getattr(context, "authorize_actor", None)
        if check is not None:
            check(connector_id, payload.get("actor"), request)

    def oauth_store():
        from openjarvis.connectors.oauth_state import OAuthStateStore

        return OAuthStateStore(context.directory())

    def prepare_oauth(connector_id, request):
        from openjarvis.connectors.oauth import (
            connector_scopes,
            get_provider_for_connector,
            pkce_pair,
        )
        from openjarvis.connectors.oauth_state import validate_callback_uri

        context.ensure(connector_id)
        provider = get_provider_for_connector(context.service(connector_id))
        if not provider:
            raise HTTPException(400, "No OAuth provider for this connector")
        creds = context.client_credentials(connector_id, provider)
        if not creds:
            raise HTTPException(400, "No client credentials configured")
        redirect_uri = (
            str(request.base_url).rstrip("/")
            + f"{router.prefix}/{connector_id}/oauth/callback"
        )
        try:
            validate_callback_uri(redirect_uri)
            verifier, challenge = pkce_pair() if provider.pkce else ("", "")
            payload = dict(
                connection_binding=context.binding(connector_id),
                provider=provider.name,
                client_id=creds[0],
                redirect_uri=redirect_uri,
                scopes=connector_scopes(provider, context.service(connector_id)),
                code_verifier=verifier,
                code_challenge=challenge,
                actor=getattr(request.state, "auth_user_id", None),
            )
            return oauth_store().create(connector_id, payload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    def launch_oauth(connector_id, request, ticket):
        from urllib.parse import urlencode, urlparse

        from fastapi.responses import RedirectResponse

        from openjarvis.connectors.oauth import get_provider_for_connector
        from openjarvis.connectors.oauth_state import (
            InvalidOAuthAttempt,
            callback_cookie,
        )

        try:
            state, browser, payload = oauth_store().launch(connector_id, ticket)
        except InvalidOAuthAttempt:
            raise HTTPException(
                400, "Authorization attempt is invalid or expired; start again"
            ) from None
        context.ensure(connector_id)
        authorize_actor(connector_id, payload, request)
        if context.binding(connector_id) != payload.get("connection_binding"):
            raise HTTPException(400, "Connection changed; start authorization again")
        provider = get_provider_for_connector(context.service(connector_id))
        if provider is None or provider.name != payload["provider"]:
            raise HTTPException(400, "OAuth provider changed; start again")
        if (
            str(request.base_url).rstrip("/")
            + f"{router.prefix}/{connector_id}/oauth/callback"
            != payload["redirect_uri"]
        ):
            raise HTTPException(400, "Authorization address changed; start again")
        params = {
            **provider.extra_auth_params,
            "client_id": payload["client_id"],
            "redirect_uri": payload["redirect_uri"],
            "response_type": "code",
            "scope": " ".join(payload["scopes"]),
            "state": state,
        }
        if payload["code_challenge"]:
            params.update(
                code_challenge=payload["code_challenge"], code_challenge_method="S256"
            )
        response = RedirectResponse(provider.auth_endpoint + "?" + urlencode(params))
        response.set_cookie(
            callback_cookie(state),
            browser,
            httponly=True,
            secure=urlparse(payload["redirect_uri"]).scheme == "https",
            samesite="lax",
            max_age=600,
            path=urlparse(payload["redirect_uri"]).path,
        )
        response.headers.update(
            {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        )
        return response

    @router.post("/{connector_id}/oauth/start")
    @serialized
    def oauth_begin(connector_id: str, request: Request):
        """Authenticated API start; no API credentials are put in popup URLs."""
        from urllib.parse import urlencode

        from fastapi.responses import JSONResponse

        attempt = prepare_oauth(connector_id, request)
        path = f"{router.prefix}/{connector_id}/oauth/launch?" + urlencode(
            {"ticket": attempt["ticket"]}
        )
        return JSONResponse(
            {"launch_path": path, "attempt_id": attempt["attempt_id"]},
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/{connector_id}/oauth/start")
    @serialized
    def oauth_start(connector_id: str, request: Request):
        """Compatibility start for clients that can send authenticated navigation."""
        return launch_oauth(
            connector_id, request, prepare_oauth(connector_id, request)["ticket"]
        )

    @router.get("/{connector_id}/oauth/status")
    def oauth_status(connector_id: str, request: Request, attempt_id: str = ""):
        from fastapi.responses import JSONResponse

        from openjarvis.connectors.oauth_state import InvalidOAuthAttempt

        try:
            status = oauth_store().status(
                connector_id, attempt_id, getattr(request.state, "auth_user_id", None)
            )
        except InvalidOAuthAttempt:
            raise HTTPException(
                400, "Authorization attempt is invalid or expired; start again"
            ) from None
        return JSONResponse(status, headers={"Cache-Control": "no-store"})

    @router.get("/{connector_id}/oauth/launch")
    def oauth_launch(connector_id: str, request: Request, ticket: str = ""):
        """One-use, one-minute handoff binds the actual external browser."""
        return launch_oauth(connector_id, request, ticket)

    @router.get("/{connector_id}/oauth/callback")
    @serialized
    def oauth_callback(connector_id: str, request: Request):
        """A state + browser-bound callback authorizes exactly one exchange."""
        from urllib.parse import urlparse

        from fastapi.responses import HTMLResponse

        from openjarvis.connectors.oauth import (
            _exchange_token,
            get_provider_for_connector,
            require_access_token,
            save_tokens,
        )
        from openjarvis.connectors.oauth_state import (
            InvalidOAuthAttempt,
            callback_cookie,
        )

        query = request.query_params
        valid_query = len(query.getlist("state")) == 1 and (
            (len(query.getlist("code")) == 1 and not query.getlist("error"))
            or (len(query.getlist("error")) == 1 and not query.getlist("code"))
        )
        state = query.get("state", "")
        if not valid_query:
            raise HTTPException(400, "Invalid authorization callback")
        try:
            cookie = callback_cookie(state)
            payload = oauth_store().consume(
                connector_id,
                state,
                request.cookies.get(cookie, ""),
                str(request.base_url).rstrip("/") + request.url.path,
            )
        except InvalidOAuthAttempt:
            raise HTTPException(
                400, "Authorization attempt is invalid or expired; start again"
            ) from None

        def result(message, status=200):
            oauth_store().finish(payload, success=status == 200)
            response = HTMLResponse(
                f"<html><body><h2>{message}</h2>"
                "<p>You can close this tab and return to OpenJarvis.</p></body></html>",
                status_code=status,
            )
            response.delete_cookie(
                cookie,
                path=urlparse(payload["redirect_uri"]).path,
                secure=urlparse(payload["redirect_uri"]).scheme == "https",
                httponly=True,
                samesite="lax",
            )
            response.headers.update(
                {
                    "Cache-Control": "no-store",
                    "Referrer-Policy": "no-referrer",
                    "Content-Security-Policy": (
                        "default-src 'none'; frame-ancestors 'none'"
                    ),
                    "X-Content-Type-Options": "nosniff",
                }
            )
            return response

        try:
            context.ensure(connector_id)
            authorize_actor(connector_id, payload, request)
        except HTTPException:
            return result("Connection is stopping; start authorization again", 409)
        if context.binding(connector_id) != payload.get("connection_binding"):
            return result("Connection changed; start authorization again", 409)
        if query.get("error"):
            return result("Authorization Failed", 400)
        code = query.get("code", "")
        if not code or len(code) > 8192:
            return result("Invalid authorization callback", 400)
        provider = get_provider_for_connector(context.service(connector_id))
        creds = context.client_credentials(connector_id, provider) if provider else None
        if (
            not provider
            or provider.name != payload["provider"]
            or not creds
            or creds[0] != payload["client_id"]
        ):
            return result(
                "Client configuration changed; start authorization again", 400
            )
        try:
            tokens = _exchange_token(
                provider,
                code,
                creds[0],
                creds[1],
                payload["redirect_uri"],
                code_verifier=payload["code_verifier"],
            )
            access_token = require_access_token(tokens)
            granted = tokens.get("scope")
            if granted is not None and (
                not isinstance(granted, str)
                or not set(granted.split()).issubset(payload["scopes"])
            ):
                raise ValueError("Unexpected granted permissions")
            expiry = float(tokens.get("expires_in", 3600))
            if not 0 < expiry <= 31536000:
                raise ValueError("Invalid token expiry")
            token_payload = {
                "access_token": access_token,
                "refresh_token": tokens.get("refresh_token", ""),
                "token_type": tokens.get("token_type", "Bearer"),
                "expires_in": tokens.get("expires_in", 3600),
                "client_id": creds[0],
                "client_secret": creds[1],
                "requested_scopes": payload["scopes"],
                "expires_at": time.time() + expiry,
            }
            authorize_actor(connector_id, payload, request)
            context.before_save(connector_id)
            save_tokens(
                str(context.token_path(connector_id, provider)),
                token_payload,
            )
        except HTTPException:
            return result("Source authorization permission changed", 403)
        except Exception:
            return result("Token Exchange Failed. Start authorization again.", 500)
        context.connected(connector_id, payload.get("actor"))
        return result("Connected!")
