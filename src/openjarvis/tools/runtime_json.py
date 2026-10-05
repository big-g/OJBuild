"""Bounded extraction from supplied JSON; no files, network or expressions."""

from __future__ import annotations

import json
import math
import re

MAX_CHARS = 32768
MAX_BYTES = 131072
MAX_DEPTH = 32
MAX_VALUES = 10000


def parse_path(config):
    if not isinstance(config, dict) or set(config) != {"path"}:
        raise ValueError("JSON extraction requires a path")
    path = config["path"]
    if (
        not isinstance(path, str)
        or not 1 <= len(path) <= 512
        or not path.startswith("/")
    ):
        raise ValueError(
            "JSON path must start with / and contain 1–512 characters; "
            "example: /forecast/temperature"
        )
    parts = path[1:].split("/")
    if len(parts) > MAX_DEPTH or any(re.search(r"~(?![01])", part) for part in parts):
        raise ValueError(
            "JSON path allows at most 32 steps; escape ~ as ~0 and / as ~1"
        )
    return [part.replace("~1", "/").replace("~0", "~") for part in parts]


def _invalid_constant(_):
    raise ValueError("JSON numbers must be finite")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON contains duplicate object keys")
        result[key] = value
    return result


def extract_json(config, params):
    parts = parse_path(config)
    if set(params) != {"input"} or not isinstance(params["input"], str):
        raise ValueError("Supply one JSON text input named input")
    text = params["input"]
    if len(text) > MAX_CHARS:
        raise ValueError("JSON input exceeds 32,768 characters")
    try:
        if len(text.encode("utf-8")) > MAX_BYTES:
            raise ValueError("JSON input exceeds 128 KiB")
    except UnicodeError:
        raise ValueError("JSON input contains invalid Unicode") from None
    # Bound nesting before the recursive standard-library decoder runs.
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise ValueError("JSON input exceeds 32 nesting levels")
        elif char in "]}":
            depth -= 1
    try:
        value = json.loads(
            text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
    except (json.JSONDecodeError, RecursionError):
        raise ValueError("Input must be valid JSON text") from None
    except ValueError:
        # Keep decoder diagnostics and input values out of returned errors.
        raise ValueError(
            "JSON contains invalid numbers or duplicate object keys"
        ) from None
    pending, count = [value], 0
    while pending:
        item = pending.pop()
        count += 1
        if count > MAX_VALUES:
            raise ValueError("JSON input exceeds 10,000 values")
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("JSON numbers must be finite")
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif (
            isinstance(value, list)
            and re.fullmatch(r"0|[1-9][0-9]*", part)
            and len(part) <= 5
            and int(part) < len(value)
        ):
            value = value[int(part)]
        else:
            raise ValueError(
                "JSON path was not found; check object keys "
                "and zero-based array indexes"
            )
    output = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    try:
        if len(output.encode("utf-8")) > MAX_BYTES:
            raise ValueError("Selected JSON value exceeds 128 KiB")
    except UnicodeError:
        raise ValueError("Selected JSON value contains invalid Unicode") from None
    return output
