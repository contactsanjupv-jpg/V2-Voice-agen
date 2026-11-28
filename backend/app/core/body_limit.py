"""
Request-body size limit (pure ASGI, so it never buffers or alters the body: webhook signatures are computed over the raw bytes).

A request is refused with 413 when its declared Content-Length exceeds the limit, or, for chunked/undeclared bodies, as soon as the
bytes actually received exceed it. Webhook paths get a larger ceiling (Retell sends full transcripts). The proxy in front should
enforce a limit too (docs/DEPLOYMENT.md); this makes the application safe on its own.
"""
import json

WEBHOOK_PREFIX = "/webhooks/"


class _TooLarge(Exception):
    pass


class BodyLimitMiddleware:
    def __init__(self, app, default_limit: int, webhook_limit: int):
        self.app = app
        self.default_limit = default_limit
        self.webhook_limit = webhook_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.webhook_limit if scope.get("path", "").startswith(WEBHOOK_PREFIX) else self.default_limit
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
            except ValueError:
                too_big = True  # a malformed length is not a request we should interpret
            if too_big:
                await self._reject(send)
                return
        seen = 0
        started = False

        async def counting_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise _TooLarge()
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _TooLarge:
            if not started:
                await self._reject(send)

    @staticmethod
    async def _reject(send) -> None:
        body = json.dumps({"detail": "Request body too large."}).encode()
        await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})
