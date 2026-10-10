"""End-to-end through the MCP tool layer (in-process)."""
import asyncio
import json

import pytest


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("POSITION_RISK_STATE", str(tmp_path / "state.json"))
    from position_risk_mcp import config, server as srv
    config.get_engine.cache_clear()
    yield srv
    config.get_engine.cache_clear()


def call(srv, name, **args):
    result = asyncio.run(srv.mcp.call_tool(name, args))
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


def test_tools_listed(server):
    tools = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"check_trade", "calculate_position_size", "record_fill", "record_close", "update_stop",
            "get_risk_state", "get_instrument_spec", "list_instruments", "get_rulebook"} <= tools
    # Nothing that lets an agent change equity or rules
    assert not any("equity" in t or "unlock" in t or "set_" in t for t in tools)


def test_full_flow(server):
    c = call(server, "check_trade", symbol="/MES", side="long", entry=6500, stop=6490, contracts=5)
    assert c["verdict"] == "APPROVED"
    f = call(server, "record_fill", approval_id=c["approval_id"], fill_price=6500)
    u = call(server, "update_stop", position_id=f["position_id"], new_stop=6495)
    assert u["verdict"] == "APPROVED"
    x = call(server, "record_close", position_id=f["position_id"], exit_price=6512)
    assert x["net_pnl"] == 12 * 5 * 5 - 1.5 * 5
    st = call(server, "get_risk_state")
    assert st["account"]["equity"] == 55000 + x["net_pnl"]


def test_option_structure_via_tool(server):
    r = call(server, "calculate_position_size", symbol="SPX",
             structure={"type": "iron_condor", "width": 5, "credit": 1.5})
    assert r["contracts"] == 1


def test_errors_surface_as_tool_errors(server):
    from mcp.server.fastmcp.exceptions import ToolError
    with pytest.raises(ToolError):
        asyncio.run(server.mcp.call_tool("record_fill", {"approval_id": "apr_nope", "fill_price": 1}))
