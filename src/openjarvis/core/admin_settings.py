"""Versioned administrator overrides; TOML remains the bootstrap fallback."""

from __future__ import annotations

import copy
import dataclasses
import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import get_args

LIVE = frozenset(
    {
        "intelligence.default_model",
        "server.model",
        "tools.storage.extraction_model",
    }
)
MODEL_FIELDS = LIVE
SECRET = re.compile(
    r"api_key|password|secret|credential|private_key|access_token|refresh_token|bearer|key_path|auth_token|(?:^|[._])(?:key|token)$",
    re.I,
)
HELP = {
    "intelligence.default_model": (
        "Server default for requests without an explicit model. "
        "Existing chat selections and task assignments remain explicit."
    ),
    "server.model": (
        "Shares the administrator default model. "
        "A CLI --model option remains authoritative after a restart."
    ),
    "tools.storage.extraction_model": (
        "Model used for background memory extraction. Leave empty to follow "
        "the server default. Using another model can evict the chat model "
        "from GPU memory."
    ),
    "tools.storage.context_max_tokens": (
        "Maximum retrieved memory tokens added to a prompt; "
        "this is separate from the model context window."
    ),
    "tools.storage.enabled": (
        "Enable automatic fact extraction. Takes effect when the memory service starts."
    ),
}
BOUNDS = {
    "intelligence.temperature": (0, 2),
    "intelligence.max_tokens": (1, 1_000_000),
    "tools.storage.max_facts": (1, 1_000_000),
    "tools.storage.context_max_tokens": (0, 1_000_000),
    "tools.storage.context_top_k": (1, 10_000),
    "server.port": (1, 65535),
}


class SettingsConflict(ValueError):
    pass


def settings_path(config):
    return Path(config._config_dir) / "administrator-settings.db"


def fields(config):
    """Describe supported leaf values without returning credentials or objects."""
    result = {}

    def walk(obj, prefix=""):
        for field in dataclasses.fields(obj):
            key = f"{prefix}.{field.name}" if prefix else field.name
            if not prefix and field.name in {
                "hardware",
                "installed_at",
                "installer_version",
                "mining",
            }:
                continue
            value = getattr(obj, field.name)
            primitive_list = False
            if isinstance(value, list):
                from openjarvis.core.config import validate_config_key

                item_types = get_args(validate_config_key(key))
                primitive_list = len(item_types) == 1 and item_types[0] in (
                    str,
                    int,
                    float,
                    bool,
                )
            if SECRET.search(key) and type(value) is not bool:
                result[key] = {
                    "key": key,
                    "editable": False,
                    "reason": "Protected credential: use the "
                    "credential/provider configuration screen.",
                }
            elif dataclasses.is_dataclass(value):
                walk(value, key)
            elif type(value) in (str, int, float, bool) or (primitive_list):
                kind = "list" if isinstance(value, list) else type(value).__name__
                result[key] = {
                    "key": key,
                    "editable": True,
                    "type": kind,
                    "value": copy.deepcopy(value),
                    "application": "live" if key in LIVE else "restart",
                    "help": HELP.get(
                        key, f"{field.name.replace('_', ' ').capitalize()} in {prefix}."
                    ),
                }
                if primitive_list:
                    result[key]["item_type"] = item_types[0].__name__
                if key in BOUNDS:
                    result[key]["minimum"], result[key]["maximum"] = BOUNDS[key]
            else:
                result[key] = {
                    "key": key,
                    "editable": False,
                    "reason": "Structured configuration: use its dedicated "
                    "management screen; generic editing is unavailable.",
                }

    walk(config)
    return result


def set_value(config, key, value):
    obj = config
    parts = key.split(".")
    for part in parts[:-1]:
        obj = getattr(obj, part)
    if type(getattr(obj, parts[-1])) is float:
        value = float(value)
    setattr(obj, parts[-1], copy.deepcopy(value))


def validate(schema, changes):
    if not isinstance(changes, dict) or len(changes) > 500:
        raise ValueError("Submit at most 500 named settings")
    for key, value in changes.items():
        spec = schema.get(key)
        if not spec or not spec["editable"]:
            raise ValueError(f"Setting {key[:120]} is not editable here")
        if value is None:  # remove override, restore bootstrap value
            continue
        kind = spec["type"]
        if kind == "list":
            valid = (
                isinstance(value, list)
                and len(value) <= 1000
                and all(type(v).__name__ == spec["item_type"] for v in value)
            )
        elif kind == "float":
            valid = type(value) in (int, float)
        else:
            valid = type(value).__name__ == kind
        if not valid:
            raise ValueError(f"{key} requires {kind}; lists contain only scalar values")
        if key in BOUNDS and not BOUNDS[key][0] <= value <= BOUNDS[key][1]:
            raise ValueError(
                f"{key} must be between {BOUNDS[key][0]} and {BOUNDS[key][1]}"
            )
        if len(json.dumps(value, allow_nan=False)) > 65536:
            raise ValueError(f"{key} exceeds the 64 KiB setting limit")
        numbers = value if isinstance(value, list) else [value]
        if any(type(v) is float and not math.isfinite(v) for v in numbers):
            raise ValueError(f"{key} requires finite numbers")
        if key in MODEL_FIELDS and (
            len(value) > 2048 or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("Use an installed model ID without control characters")


class AdminSettingsStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS administrator_settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL,
                    overrides TEXT NOT NULL);
                INSERT OR IGNORE INTO administrator_settings VALUES(1,1,'{}');
                CREATE TABLE IF NOT EXISTS administrator_settings_audit (
                    seq INTEGER PRIMARY KEY, actor TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    revision INTEGER NOT NULL, fields TEXT NOT NULL);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def read(self):
        with self.connection() as db:
            revision, overrides = db.execute(
                "SELECT revision,overrides FROM administrator_settings WHERE id=1"
            ).fetchone()
        return revision, json.loads(overrides)

    def update(self, revision, changes, actor, schema):
        validate(schema, changes)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current, raw = db.execute(
                "SELECT revision,overrides FROM administrator_settings WHERE id=1"
            ).fetchone()
            if revision != current:
                raise SettingsConflict(
                    "Settings changed in another session. Reload before saving."
                )
            overrides = json.loads(raw)
            for key, value in changes.items():
                if value is None:
                    overrides.pop(key, None)
                else:
                    overrides[key] = value
            db.execute(
                "UPDATE administrator_settings SET revision=?,overrides=? WHERE id=1",
                (current + 1, json.dumps(overrides, allow_nan=False)),
            )
            db.execute(
                "INSERT INTO administrator_settings_audit"
                "(actor,timestamp,revision,fields) VALUES(?,?,?,?)",
                (actor, time.time(), current + 1, json.dumps(sorted(changes))),
            )
        return self.read()


def apply_saved_settings(config):
    path = settings_path(config)
    if not path.exists():
        return
    _, overrides = AdminSettingsStore(path).read()
    validate(fields(config), overrides)
    for key, value in overrides.items():
        set_value(config, key, value)
