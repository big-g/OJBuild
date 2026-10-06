"""Explicit server-bound model identities and live inference gates."""

from __future__ import annotations

import asyncio
import copy
import re
from urllib.parse import quote

import httpx

from openjarvis.engine._base import EngineConnectionError, InferenceEngine
from openjarvis.engine.connection_discovery import inspect_model
from openjarvis.engine.connection_store import ConnectionConflict, definition
from openjarvis.engine.ollama import OllamaEngine

PREFIX = "oj/"


class ConfiguredModelError(EngineConnectionError):
    """A safe user-facing failure confined to the selected backend."""

    def __init__(self, message, status_code=503):
        super().__init__(message)
        self.status_code = status_code


class ConfiguredModelUnavailable(ConfiguredModelError):
    """Transport failure before inference; eligible for explicit fallback."""


def model_id(connection_id, serving_id):
    return f"{PREFIX}{connection_id}/{quote(serving_id, safe='')}"


def selectable_models(store):
    if store is None:
        return []
    models = []
    for connection in store.list():
        if (
            not connection["enabled"]
            or connection["adapter_id"] != "ollama"
            or connection["config_version"] != 1
            or connection["discovery_state"] != "discovered"
        ):
            continue
        for model in connection["catalog"]:
            if model.get(
                "capability_state"
            ) != "reported" or "completion" not in model.get("capabilities", []):
                continue
            models.append(
                {
                    "id": model_id(connection["id"], model["serving_id"]),
                    "owned_by": "configured_ollama",
                    "serving_id": model["serving_id"],
                    "connection_id": connection["id"],
                    "connection_name": connection["name"],
                    "display_name": f"{model['serving_id']} — {connection['name']}",
                    "capabilities": model["capabilities"],
                    "capability_state": "reported",
                }
            )
    return models


class ConfiguredModelEngine(InferenceEngine):
    """A single request binds one model to one configuration revision."""

    engine_id = "ollama"

    def __init__(self, store, selected):
        parts = selected.split("/", 2)
        if (
            len(parts) != 3
            or parts[0] != "oj"
            or not re.fullmatch(r"[a-f0-9]{32}", parts[1])
        ):
            raise ConfiguredModelError(
                "Invalid configured model selection. Refresh the model list."
            )
        try:
            self.connection = store.get(parts[1])
        except KeyError:
            raise ConfiguredModelError(
                "Selected model server was removed. Choose another model."
            ) from None
        self.store, self.selected = store, selected
        self.serving_id = next(
            (
                m["serving_id"]
                for m in self.connection["catalog"]
                if model_id(parts[1], m["serving_id"]) == selected
            ),
            None,
        )
        if self.serving_id is None:
            raise ConfiguredModelError(
                "Selected model is no longer in its server catalog. "
                "Refresh the model list."
            )
        self._row()

    def _row(self):
        validator = getattr(self, "validate_binding", None)
        if validator is not None:
            validator()
        try:
            row = self.store.get(self.connection["id"], self.connection["revision"])
        except (KeyError, ConnectionConflict):
            raise ConfiguredModelError(
                "Selected model connection changed or was removed. "
                "Refresh the model list."
            ) from None
        if (
            not row["enabled"]
            or row["discovery_state"] != "discovered"
            or row["adapter_id"] != "ollama"
            or row["config_version"] != 1
        ):
            raise ConfiguredModelError(
                "Selected model connection is disabled or unsupported. "
                "Choose another model."
            )
        definition(row["name"], row["url"])
        return row

    def check(self, *, tools=False, images=False, live=False):
        row = self._row()
        model = next(
            (m for m in row["catalog"] if m["serving_id"] == self.serving_id), {}
        )
        caps = model.get("capabilities", [])
        if model.get("capability_state") != "reported":
            raise ConfiguredModelError(
                "Read this model's capabilities before using it for chat."
            )
        if live:
            try:
                caps = inspect_model(row, self.serving_id)
            except (httpx.HTTPError, OSError):
                raise ConfiguredModelUnavailable(
                    "Selected Ollama server/model is unavailable. Check the "
                    "connection and read capabilities again."
                ) from None
            except (ValueError, RecursionError):
                raise ConfiguredModelError(
                    "Selected model returned an invalid capability manifest. "
                    "Review its capabilities before using it."
                ) from None
            self._row()
        if "completion" not in caps:
            raise ConfiguredModelError(
                "Selected model does not report chat completion support."
            )
        if tools and "tools" not in caps:
            raise ConfiguredModelError(
                "Selected model does not report tool-calling support. "
                "Choose a tool-capable model."
            )
        if tools and getattr(self, "diagnostic_tools_passed", True) is False:
            raise ConfiguredModelError("Assigned model lacks a passing tool-call probe")
        if images and "vision" not in caps:
            raise ConfiguredModelError(
                "Selected model does not report image support. Choose a vision model."
            )

    def _prepare(self, messages, model, kwargs):
        if model != self.selected:
            raise ConfiguredModelError(
                "This request is bound to a different model server."
            )
        self.check(
            tools=bool(kwargs.get("tools")),
            images=any(getattr(m, "images", None) for m in messages),
            live=True,
        )
        options = dict(kwargs)
        if options.get("tools"):
            # A provider rejection must never strip the requested tools and retry.
            options["require_tools"] = True
        return options

    def _engine(self):
        return OllamaEngine(host=self.connection["url"], timeout=180, trust_env=False)

    def generate(self, messages, *, model, temperature=0.7, max_tokens=1024, **kwargs):
        options = self._prepare(messages, model, kwargs)
        engine = self._engine()
        try:
            result = engine.generate(
                messages,
                model=self.serving_id,
                temperature=temperature,
                max_tokens=max_tokens,
                **options,
            )
            result["model"] = self.selected
            return result
        except (EngineConnectionError, httpx.HTTPError, RuntimeError, ValueError):
            raise ConfiguredModelError(
                "Selected Ollama model failed to generate. Check its availability, "
                "context budget and requested capabilities. No other server was used.",
                status_code=502,
            ) from None
        finally:
            engine.close()

    async def stream_full(
        self, messages, *, model, temperature=0.7, max_tokens=1024, **kwargs
    ):
        options = await asyncio.to_thread(self._prepare, messages, model, kwargs)
        engine = self._engine()
        try:
            async for chunk in engine.stream_full(
                messages,
                model=self.serving_id,
                temperature=temperature,
                max_tokens=max_tokens,
                **options,
            ):
                yield chunk
        except (EngineConnectionError, httpx.HTTPError, RuntimeError, ValueError):
            raise ConfiguredModelError(
                "Selected Ollama model failed during streaming. Check its availability "
                "and requested capabilities. No other server was used.",
                status_code=502,
            ) from None
        finally:
            client = engine._async_client
            if client is not None:
                await client.aclose()
            engine.close()

    async def stream(self, messages, *, model, **kwargs):
        async for chunk in self.stream_full(messages, model=model, **kwargs):
            if chunk.content:
                yield chunk.content

    def list_models(self):
        return [self.selected]

    def health(self):
        try:
            self.check(live=True)
            return True
        except EngineConnectionError:
            return False


def preserve_wrappers(original, replacement):
    """Copy known safety/telemetry wrappers without mutating shared engines."""
    from openjarvis.security.guardrails import GuardrailsEngine
    from openjarvis.telemetry.instrumented_engine import InstrumentedEngine

    if isinstance(original, InstrumentedEngine):
        result = copy.copy(original)
        result._inner = preserve_wrappers(original._inner, replacement)
        return result
    if isinstance(original, GuardrailsEngine):
        result = copy.copy(original)
        result._engine = preserve_wrappers(original._engine, replacement)
        return result
    from openjarvis.engine.multi import MultiEngine

    if isinstance(original, MultiEngine):
        # serve() registers the active, security-wrapped engine first. Preserve
        # that contract instead of treating the container as an unwrapped leaf.
        if not original._engines:
            raise ConfiguredModelError("No active inference safety chain available.")
        return preserve_wrappers(original._engines[0][1], replacement)
    # Ordinary registered engines are the serving leaf.
    # Unknown wrappers cannot silently lose their safety contract.
    if any(name in vars(original) for name in ("_inner", "_engine")):
        raise ConfiguredModelError(
            "Unsupported inference wrapper for configured models."
        )
    return replacement
