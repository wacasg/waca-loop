"""Smoke test: the MCP server registers the three core tools (§4)."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_build_mcp_registers_three_tools(context):
    pytest.importorskip("mcp")  # skip if the MCP SDK isn't installed
    from waca_loop.server import build_mcp

    server = build_mcp(context)
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert {"lookup_terms", "log_usage", "get_company_context"} <= names


def test_get_company_context_summarizes(context):
    out = context.get_company_context("サンプル協会")
    assert "used_terms" in out
    assert "context" in out
    assert isinstance(out["used_terms"], list)
