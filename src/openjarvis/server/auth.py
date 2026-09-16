"""Human authentication context for the OpenJarvis API."""

from __future__ import annotations

from fastapi import HTTPException, Request

from openjarvis.server.auth_store import AuthStore


SESSION_HEADER = "X-OpenJarvis-Session"


def get_auth_store(request: Request) -> AuthStore:
    """Return the application's authentication store."""
    store = getattr(request.app.state, "auth_store", None)

    if store is None:
        store = AuthStore()
        request.app.state.auth_store = store

    return store


def authenticate_request(request: Request) -> str:
    """Authenticate a request and return its canonical user_id.

    Human authentication is intentionally separate from the existing
    master OPENJARVIS_API_KEY authentication.
    """
    token = request.headers.get(SESSION_HEADER, "").strip()

    if not token:
        raise HTTPException(
            status_code=401,
            detail="Missing OpenJarvis session",
            headers={"WWW-Authenticate": "Session"},
        )

    store = get_auth_store(request)
    user = store.get_user_for_token(token)

    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired OpenJarvis session",
            headers={"WWW-Authenticate": "Session"},
        )

    user_id = str(user["user_id"])

    # Make the authenticated identity available to downstream routes.
    request.state.auth_user_id = user_id
    request.state.auth_user = user

    return user_id


def get_authenticated_user_id(request: Request) -> str:
    """Return the authenticated user_id for an API request."""
    user_id = getattr(request.state, "auth_user_id", None)

    if not user_id:
        return authenticate_request(request)

    return str(user_id)
