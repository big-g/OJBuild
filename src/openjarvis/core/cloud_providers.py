"""Server cloud-provider credentials, backed by the existing encrypted vault."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone

from openjarvis.core.paths import get_config_dir

PROVIDERS = (
    ("openai", "OpenAI", "OPENAI_API_KEY"),
    ("anthropic", "Anthropic", "ANTHROPIC_API_KEY"),
    ("google", "Google Gemini", "GEMINI_API_KEY"),
    ("openrouter", "OpenRouter", "OPENROUTER_API_KEY"),
    ("minimax", "MiniMax", "MINIMAX_API_KEY"),
    ("deepseek", "DeepSeek", "DEEPSEEK_API_KEY"),
)
KEY_NAMES = frozenset(item[2] for item in PROVIDERS)
_LOCK = threading.RLock()


class ProviderConflict(ValueError):
    pass


def _vault():
    from openjarvis.connectors.token_vault import TokenVault

    return TokenVault(get_config_dir() / "cloud-provider-keys.json")


def _managed():
    if not (get_config_dir() / "cloud-provider-keys.json").exists():
        return None
    values = _vault().load()
    if (
        not isinstance(values, dict)
        or type(values.get("revision")) is not int
        or not isinstance(values.get("keys"), dict)
        or set(values["keys"]) - KEY_NAMES
        or any(not isinstance(value, str) for value in values["keys"].values())
        or not isinstance(values.get("audit"), list)
    ):
        raise ValueError("Invalid cloud provider credential reference")
    return values


def resolve_keys():
    """Managed values (including removal tombstones) override legacy settings."""
    keys = {}
    legacy = get_config_dir() / "cloud-keys.env"
    if legacy.exists():
        for line in legacy.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                name, value = line.split("=", 1)
                if name.strip() in KEY_NAMES | {
                    "GOOGLE_API_KEY",
                    "OPENAI_CODEX_API_KEY",
                }:
                    keys[name.strip()] = value.strip()
    for name in KEY_NAMES | {"GOOGLE_API_KEY", "OPENAI_CODEX_API_KEY"}:
        if os.environ.get(name):
            keys[name] = os.environ[name]
    if not keys.get("GEMINI_API_KEY") and keys.get("GOOGLE_API_KEY"):
        keys["GEMINI_API_KEY"] = keys["GOOGLE_API_KEY"]
    with _LOCK:
        values = _managed()
        if values:
            keys.update(values.get("keys", {}))
            # Managed Gemini removal must also suppress its old environment alias.
            if "GEMINI_API_KEY" in values.get("keys", {}):
                keys.pop("GOOGLE_API_KEY", None)
    return keys


def public_settings():
    with _LOCK:
        values = _managed() or {}
        effective = resolve_keys()
        managed = values.get("keys", {})
        return {
            "revision": values.get("revision", 0),
            "providers": [
                {
                    "id": identity,
                    "name": name,
                    "key_name": key,
                    "configured": bool(effective.get(key)),
                    "source": "managed"
                    if key in managed
                    else "legacy"
                    if effective.get(key)
                    else "none",
                }
                for identity, name, key in PROVIDERS
            ],
            "audit": values.get("audit", []),
        }


def change_key(identity, secret, revision, actor):
    key = next((key for item, _, key in PROVIDERS if item == identity), None)
    if key is None:
        raise ValueError("Unknown cloud provider")
    if secret is not None and (
        not isinstance(secret, str)
        or not 1 <= len(secret) <= 8192
        or any(ord(c) < 33 or ord(c) > 126 for c in secret)
    ):
        raise ValueError(
            "Enter a key of 1–8192 printable characters without whitespace"
        )
    with _LOCK:
        vault = _vault()
        values = vault.load()
        if values is None:
            values = {"revision": 0, "keys": {}, "audit": []}
        if values["revision"] != revision:
            raise ProviderConflict("Provider settings changed; reload before saving")
        values["keys"][key] = secret or ""
        values["revision"] += 1
        values["audit"] = (
            values["audit"]
            + [
                {
                    "actor": actor,
                    "provider": identity,
                    "action": "save" if secret else "remove",
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            ]
        )[-200:]
        vault.save(values, expected_revision=revision)
        return public_settings()
