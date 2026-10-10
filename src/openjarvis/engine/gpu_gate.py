"""Coordinate Ollama requests with an optional local GPU worker."""

from __future__ import annotations

import functools
import inspect
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

from openjarvis.engine._base import EngineConnectionError

_background = ContextVar("gpu_background", default=None)


def background_options():
    return {"num_ctx": 2048} if _background.get() else {}


@contextmanager
def gpu_reservation(host=None):
    if host is not None:
        endpoint = urlsplit(host)
        if endpoint.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        } or endpoint.port not in {None, 11434}:
            yield None
            return
    path = os.environ.get("OPENJARVIS_GPU_LOCK_PATH")
    if not path:
        yield None
        return
    import fcntl

    fd = None
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except (OSError, ValueError) as exc:
        if fd is not None:
            os.close(fd)
        raise EngineConnectionError(
            "GPU is switching models or reserved for 3D generation; retry shortly."
        ) from exc
    try:
        try:
            metadata_fd = os.open(path + ".json", os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            lease = None
        else:
            with os.fdopen(metadata_fd) as source:
                lease = json.load(source)
            if not isinstance(lease, dict) or not isinstance(
                lease.get("background_model"), str
            ):
                raise ValueError("Invalid GPU lease")
        yield lease
    except (OSError, ValueError) as exc:
        raise EngineConnectionError(
            "GPU coordination unavailable; contact the administrator."
        ) from exc
    finally:
        os.close(fd)


def _arguments(function, args, kwargs, lease):
    if not lease:
        return args, kwargs
    bound = inspect.signature(function).bind(*args, **kwargs)
    bound.apply_defaults()
    if "model" not in bound.arguments:
        raise EngineConnectionError(
            "Additional local models are paused during 3D generation."
        )
    bound.arguments["model"] = lease["background_model"]
    if "max_tokens" in bound.arguments:
        bound.arguments["max_tokens"] = min(bound.arguments["max_tokens"], 1024)
    # Keep optional caller options from increasing VRAM or enabling extended thinking.
    if "kwargs" in bound.arguments:
        bound.arguments["kwargs"] = {
            **bound.arguments["kwargs"],
            "num_ctx": 2048,
            "think": False,
        }
    return bound.args, bound.kwargs


def _host(args):
    instance = args[0] if args else None
    return (
        getattr(instance, "_host", None)
        or getattr(instance, "_base_url", None)
        or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    )


def gpu_optional_guard(function):
    guarded = gpu_guard(function)

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return guarded(*args, **kwargs)
        except EngineConnectionError:
            return None  # Connector search retains its keyword-search fallback.

    return wrapped


def gpu_guard(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with gpu_reservation(_host(args)) as lease:
            if lease:
                raise EngineConnectionError(
                    "Additional local models are paused during 3D generation."
                )
            return function(*args, **kwargs)

    return wrapped


def gpu_chat_guard(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with gpu_reservation(_host(args)) as lease:
            args, kwargs = _arguments(function, args, kwargs, lease)
            token = _background.set(lease)
            try:
                return function(*args, **kwargs)
            finally:
                _background.reset(token)

    return wrapped


def gpu_stream_guard(function):
    @functools.wraps(function)
    async def wrapped(*args, **kwargs):
        with gpu_reservation(_host(args)) as lease:
            args, kwargs = _arguments(function, args, kwargs, lease)
            # Set the context only while advancing the source; no context leaks
            # across yields, stream cancellation or async-generator finalization.
            iterator = function(*args, **kwargs)
            try:
                while True:
                    token = _background.set(lease)
                    try:
                        item = await anext(iterator)
                    except StopAsyncIteration:
                        break
                    finally:
                        _background.reset(token)
                    yield item
            finally:
                await iterator.aclose()

    return wrapped
