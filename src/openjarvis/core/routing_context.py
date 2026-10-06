"""Request-local routing evidence, distinct from identity and permissions."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

_ROUTING = ContextVar("model_routing", default=None)


def routing_metadata():
    value = _ROUTING.get()
    return {"routing": deepcopy(value)} if value else {}


def bind_routing(decision):
    """Bind within an enclosing routing_scope; callers own its lifetime."""
    _ROUTING.set(decision)


@contextmanager
def routing_scope(decision):
    token = _ROUTING.set(decision)
    try:
        yield decision
    finally:
        _ROUTING.reset(token)
