"""Default-deny system mutations; account-owned operations keep their own checks."""

from __future__ import annotations

# These handlers independently verify human identity and ownership or validate
# input-only actions. Adding a new system mutation requires administrator access.
_PERSONAL_PREFIXES = ("/v1/sessions", "/v1/projects", "/v1/sources", "/v1/files")
_PERSONAL_ACTIONS = frozenset(
    {
        "/v1/chat/completions",
        "/v1/research",
        "/api/research",
        "/v1/speech/transcribe",
        "/v1/feedback",
        "/v1/auth/login",
        "/v1/auth/logout",
        "/v1/auth/recover",
        "/v1/auth/password",
        "/v1/auth/recovery-code",
    }
)
_PRIVATE_SYSTEM_PREFIXES = (
    "/v1/agents",
    "/v1/managed-agents",
    "/v1/templates",
    "/v1/traces",
    "/v1/skills",
    "/v1/memory",
    "/v1/learning",
    "/v1/optimize",
    "/v1/channels",
    "/v1/approvals",
    "/v1/connectors",
    "/v1/runtime-tools",
    "/v1/runtime-mcp",
    "/v1/auth/users",
    "/v1/security",
)


def _within(path, prefix):
    return path == prefix or path.startswith(prefix + "/")


def requires_admin(path: str, method: str) -> bool:
    path = path.rstrip("/") or "/"
    if any(_within(path, prefix) for prefix in _PRIVATE_SYSTEM_PREFIXES):
        return True
    if method in {"GET", "HEAD", "OPTIONS"}:
        return False
    if not (_within(path, "/v1") or _within(path, "/api")):
        return False  # Webhooks have their own provider signature checks.
    if path in _PERSONAL_ACTIONS or any(_within(path, p) for p in _PERSONAL_PREFIXES):
        return False
    return True
