"""ASGI request correlation spans response streaming and disconnect cleanup."""

from openjarvis.core.correlation import ExecutionIdentity, execution_scope

CORRELATION_HEADERS = ("X-Request-ID", "X-Turn-ID", "X-Trace-ID")


class CorrelationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        # Never accept client-supplied user/session/request/trace identity headers.
        identity = ExecutionIdentity()
        scope.setdefault("state", {})["execution_identity"] = identity

        async def correlated_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {name.lower().encode() for name in CORRELATION_HEADERS}
                headers = [
                    (key, value) for key, value in headers if key.lower() not in names
                ]
                headers.extend(
                    (name.lower().encode(), value.encode())
                    for name, value in zip(
                        CORRELATION_HEADERS,
                        (identity.request_id, identity.turn_id, identity.trace_id),
                    )
                )
                message = {**message, "headers": headers}
            await send(message)

        with execution_scope(identity):
            await self.app(scope, receive, correlated_send)
