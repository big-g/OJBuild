"""Carry active Ollama configuration into routing; never execute or pull models."""

import time

import httpx

from openjarvis.engine.connection_discovery import discover, inspect_model
from openjarvis.engine.connection_store import definition
from openjarvis.engine.multi import MultiEngine
from openjarvis.engine.ollama import OllamaEngine
from openjarvis.security.guardrails import GuardrailsEngine
from openjarvis.telemetry.instrumented_engine import InstrumentedEngine


def backend_endpoints(engine):
    seen, endpoints = set(), []

    def visit(current):
        if id(current) in seen:
            return
        seen.add(id(current))
        if isinstance(current, OllamaEngine):
            if current._host not in endpoints:
                endpoints.append(current._host)
        elif isinstance(current, MultiEngine):
            for _, inner in current._engines:
                visit(inner)
        elif isinstance(current, GuardrailsEngine):
            visit(current._engine)
        elif isinstance(current, InstrumentedEngine):
            visit(current._inner)

    visit(engine)
    return endpoints


def inherit_backend_models(store, engine, actor, authorize):
    """Read bounded metadata first, then reauthorize before each atomic write."""
    changed, notices, seen = 0, [], set()
    deadline = time.monotonic() + 30
    manifests = 0
    endpoints = backend_endpoints(engine)
    if len(endpoints) > 4:
        notices.append("Only four backend servers can be refreshed at once.")
    for raw in endpoints[:4]:
        try:
            endpoint = definition("backend_ollama", raw)["url"]
        except ValueError:
            notices.append(
                "A backend Ollama URL needs an explicit private IP or localhost "
                "before routing can inherit it."
            )
            continue
        if endpoint in seen:
            continue
        seen.add(endpoint)
        expected = store.backend_target(endpoint)
        if expected["managed"]:
            notices.append(
                "An administrator changed or removed a backend connection; "
                "its override is preserved."
            )
            continue
        if time.monotonic() >= deadline:
            notices.append(
                "Backend metadata refresh reached its time limit. Refresh to retry."
            )
            break
        connection = {"name": "backend_ollama", "url": endpoint}
        try:
            connection["catalog"] = discover(connection)
        except (ValueError, httpx.HTTPError):
            notices.append(
                "Cannot read a backend Ollama catalog. "
                "Check its availability and refresh."
            )
            continue
        incomplete = False
        for model in connection["catalog"]:
            if manifests >= 32 or time.monotonic() >= deadline:
                incomplete = True
                continue
            manifests += 1
            try:
                model["capabilities"] = inspect_model(connection, model["serving_id"])
                model["capability_state"] = "reported"
                model["capabilities_at"] = time.time()
            except (ValueError, httpx.HTTPError):
                incomplete = True
        if incomplete:
            notices.append(
                "Some backend model capabilities could not be verified. Refresh "
                "or read their capabilities in Model server connections."
            )
        authorize()
        changed += int(
            store.inherit_backend(endpoint, connection["catalog"], expected, actor)
        )
    return {"changed": changed, "notices": list(dict.fromkeys(notices))}
