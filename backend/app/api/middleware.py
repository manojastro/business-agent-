"""Rate limiting, request-size limits and security headers (pure ASGI, streaming-safe)."""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _Bucket:
    __slots__ = ("tokens", "updated")

    def __init__(self, capacity: float) -> None:
        self.tokens = capacity
        self.updated = time.monotonic()


class RateLimiter:
    """Token bucket per client and route class. In-process: with several API replicas, put a
    shared limiter (proxy or Redis) in front - documented in SECURITY.md."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.buckets: dict[str, _Bucket] = defaultdict(lambda: _Bucket(per_minute))
        self.lock = threading.Lock()

    def allow(self, key: str, capacity: int) -> bool:
        with self.lock:
            b = self.buckets.setdefault(key, _Bucket(capacity))
            now = time.monotonic()
            b.tokens = min(capacity, b.tokens + (now - b.updated) * capacity / 60)
            b.updated = now
            if b.tokens >= 1:
                b.tokens -= 1
                return True
            return False


SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
]


def _error(status: int, code: str, message: str) -> tuple[Message, Message]:
    body = json.dumps({"error": {"code": code, "message": message, "details": None}}).encode()
    start: Message = {"type": "http.response.start", "status": status,
                      "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]}
    return start, {"type": "http.response.body", "body": body}


class GuardMiddleware:
    def __init__(self, app: ASGIApp, per_minute: int, max_body: int) -> None:
        self.app = app
        self.limiter = RateLimiter(per_minute)
        self.max_body = max_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        client = (scope.get("client") or ("unknown", 0))[0]
        is_login = path.endswith("/auth/login")
        capacity = 10 if is_login else self.limiter.per_minute
        if path.startswith("/api/") and not path.endswith(("/health/live", "/health/ready")) and "/events" not in path:
            if not self.limiter.allow(f"{client}:{'login' if is_login else 'api'}", capacity):
                for m in _error(429, "rate_limited", "too many requests; slow down"):
                    await send(m)
                return
        headers = dict(scope.get("headers") or [])
        length = headers.get(b"content-length")
        if length is not None and int(length or 0) > self.max_body:
            for m in _error(413, "payload_too_large", f"request body exceeds {self.max_body} bytes"):
                await send(m)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > self.max_body:
                    raise ValueError("request body too large")
            return msg

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + SECURITY_HEADERS
            await send(message)

        await self.app(scope, limited_receive, send_with_headers)
