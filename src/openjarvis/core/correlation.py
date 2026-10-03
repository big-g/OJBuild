"""Trusted execution identity scoped to one request/turn, including worker threads."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from uuid import uuid4


@dataclass(frozen=True)
class ExecutionIdentity:
    request_id: str = field(default_factory=lambda: uuid4().hex)
    turn_id: str = field(default_factory=lambda: uuid4().hex)
    trace_id: str = field(default_factory=lambda: uuid4().hex)
    user_id: str = ""
    session_id: str = ""
    conversation_id: str = ""

    def metadata(self):
        return asdict(self)


_IDENTITY: ContextVar[ExecutionIdentity | None] = ContextVar(
    "execution_identity", default=None
)


def current_identity():
    return _IDENTITY.get()


@contextmanager
def execution_scope(identity):
    token = _IDENTITY.set(identity)
    try:
        yield identity
    finally:
        _IDENTITY.reset(token)


def bind_verified_identity(*, user_id, session_id=""):
    """Call only after authentication and, if present, session ownership checks."""
    identity = replace(
        current_identity() or ExecutionIdentity(),
        user_id=user_id,
        session_id=session_id,
        conversation_id=session_id,
    )
    _IDENTITY.set(identity)
    return identity
