"""Bounded, administrator-triggered Ollama catalog reads. No model execution."""

import json
import time

import httpx

from openjarvis.engine.connection_store import definition

MAX_CATALOG_BYTES = 1_048_576


def read_json(connection, path, method="GET", body=None):
    endpoint = definition(connection["name"], connection["url"])["url"]
    started = time.monotonic()
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        with client.stream(
            method,
            endpoint + path,
            headers={"Accept-Encoding": "identity"},
            **({"json": body} if body is not None else {}),
        ) as response:
            if response.status_code != 200:
                raise ValueError("Catalog request did not succeed")
            if (
                response.headers.get("content-encoding", "identity").lower()
                != "identity"
            ):
                raise ValueError("Compressed catalog responses are unsupported")
            if (
                response.headers.get("content-type", "").split(";", 1)[0].lower()
                != "application/json"
            ):
                raise ValueError("Catalog must be JSON")
            body = bytearray()
            for chunk in response.iter_bytes(chunk_size=65_536):
                if (
                    len(body) + len(chunk) > MAX_CATALOG_BYTES
                    or time.monotonic() - started > 10
                ):
                    raise ValueError("Catalog exceeds discovery limits")
                body.extend(chunk)
    return json.loads(body)


def discover(connection):
    payload = read_json(connection, "/api/tags")
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list) or len(models) > 500:
        raise ValueError("Invalid or oversized model catalog")
    catalog, seen = [], set()
    for item in models:
        if not isinstance(item, dict):
            raise ValueError("Invalid model entry")
        name = item.get("name")
        size = item.get("size", 0)
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 256
            or not name.isprintable()
            or any(char.isspace() for char in name)
            or name in seen
            or type(size) is not int
            or size < 0
            or size > 2**63 - 1
        ):
            raise ValueError("Invalid or duplicate model entry")
        seen.add(name)
        # /api/tags establishes presence only, not verified tool/vision capability.
        catalog.append(
            {"serving_id": name, "size_bytes": size, "capability_state": "unknown"}
        )
    return catalog


def inspect_model(connection, serving_id):
    if not any(m["serving_id"] == serving_id for m in connection["catalog"]):
        raise ValueError("Model is not in this connection's catalog")
    payload = read_json(connection, "/api/show", "POST", {"model": serving_id})
    caps = payload.get("capabilities") if isinstance(payload, dict) else None
    if (
        not isinstance(caps, list)
        or len(caps) > 16
        or not all(
            isinstance(c, str) and 1 <= len(c) <= 64 and c.isascii() and c.isalpha()
            for c in caps
        )
        or len(set(caps)) != len(caps)
    ):
        raise ValueError("Ollama did not return a valid capability manifest")
    return sorted(caps)
