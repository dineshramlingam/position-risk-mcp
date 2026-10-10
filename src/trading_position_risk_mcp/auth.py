"""Bearer-token guard for the HTTP transport (pure ASGI, no framework dependency)."""
from __future__ import annotations

import hmac
import ipaddress

TOKEN_ENV = "POSITION_RISK_TOKEN"
MIN_TOKEN_LEN = 24


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_bind(host: str, token: str | None) -> None:
    """Refuse to expose the tools beyond this machine without a token."""
    if token is not None and len(token) < MIN_TOKEN_LEN:
        raise SystemExit(f"{TOKEN_ENV} must be at least {MIN_TOKEN_LEN} characters")
    if not is_loopback(host) and not token:
        raise SystemExit(f"Refusing to listen on {host} without authentication. Set {TOKEN_ENV} "
                         f"(>= {MIN_TOKEN_LEN} chars) and terminate TLS in front of the server.")


class BearerAuth:
    def __init__(self, app, token: str | None):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if self.token is None or scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        supplied = ""
        for k, v in scope.get("headers", []):
            if k == b"authorization":
                supplied = v.decode("latin-1")
                break
        scheme, _, cred = supplied.partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(cred.encode(), self.token.encode()):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"text/plain"), (b"www-authenticate", b"Bearer")]})
        await send({"type": "http.response.body", "body": b"unauthorized"})
