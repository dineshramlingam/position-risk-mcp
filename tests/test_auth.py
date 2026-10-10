import asyncio

import pytest

from trading_position_risk_mcp.auth import BearerAuth, check_bind

TOKEN = "x" * 32


def call(app, headers):
    sent = []

    async def send(m):
        sent.append(m)

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})

    asyncio.run(BearerAuth(inner, TOKEN)({"type": "http", "headers": headers}, None, send))
    return sent[0]["status"]


def test_bearer():
    assert call(None, []) == 401
    assert call(None, [(b"authorization", b"Bearer wrong")]) == 401
    assert call(None, [(b"authorization", f"Bearer {TOKEN}".encode())]) == 200


def test_bind_rules():
    check_bind("127.0.0.1", None)
    check_bind("localhost", None)
    check_bind("0.0.0.0", TOKEN)
    with pytest.raises(SystemExit):
        check_bind("0.0.0.0", None)
    with pytest.raises(SystemExit):
        check_bind("127.0.0.1", "short")
