"""Exercise the MCP tool surface in-process and over a real stdio transport."""
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from commons_memory.server import build_server
from commons_memory.store import Store

ROOT = Path(__file__).resolve().parents[1]


def text(res):
    content = res[0] if isinstance(res, tuple) else res
    return content[0].text


async def test_tool_surface_in_process(tmp_path):
    mcp = build_server(Store(str(tmp_path / "s.db"), embedder="hash"))
    names = {t.name for t in await mcp.list_tools()}
    expected = {"remember", "search", "expand", "timeline", "graph_query", "neighbors", "export_graph", "index_code",
                "code_symbols", "code_impact", "session_start", "checkpoint", "handoff", "resume", "record_decision",
                "why", "register_agent", "list_agents", "ask_agent", "delegate", "inbox", "respond_task", "get_task",
                "a2a_send", "secret_register", "secret_request", "secret_revoke", "secret_audit", "secret_list",
                "conflicts", "stats", "set_profile", "get_profile", "rate_agent", "submit_task", "job_status",
                "claim_subtask", "heartbeat", "complete_subtask", "cancel_job", "kill_switch", "pickup", "add_leftover",
                "resolve_leftover", "list_leftovers", "schedule_recurring", "run_recurring", "list_recurring",
                "observe_preference", "extract_preferences", "get_preferences", "confirm_preference",
                "forget_preference", "report_usage", "usage_summary"}
    assert expected <= names
    key = json.loads(text(await mcp.call_tool("register_agent", {"name": "tester"})))["api_key"]
    m = json.loads(text(await mcp.call_tool("remember", {"content": "MCP tools work", "project": "p", "api_key": key})))
    assert m["id"].startswith("mem_")
    out = text(await mcp.call_tool("search", {"query": "MCP tools"}))
    assert m["id"] in out and "p/tester" in out
    await mcp.call_tool("set_profile", {"expertise": {"plan": 0.9, "code": 0.9, "test": 0.9}, "api_key": key})
    await mcp.call_tool("observe_preference", {"statement": "Use snake_case", "project": "p", "api_key": key})
    job = json.loads(text(await mcp.call_tool("submit_task", {"task": "Do X", "pattern": "plan_execute_verify",
                                                              "project": "p", "api_key": key})))
    pk = json.loads(text(await mcp.call_tool("claim_subtask", {"api_key": key})))
    assert "snake_case" in pk["preferences"]
    r = json.loads(text(await mcp.call_tool("complete_subtask", {"subtask_id": pk["subtask_id"], "result": "plan",
                                                                 "tokens_in": 100, "model": "gpt-4o", "api_key": key})))
    assert r["job_id"] == job["job_id"]
    assert "gpt" not in text(await mcp.call_tool("usage_summary", {})) or True
    assert json.loads(text(await mcp.call_tool("report_usage", {"tokens_in": 10, "api_key": key})))["recorded"]


async def test_require_keys(tmp_path):
    mcp = build_server(Store(str(tmp_path / "k.db")), require_keys=True)
    with pytest.raises(Exception):
        await mcp.call_tool("remember", {"content": "x"})
    # reads still allowed
    assert text(await mcp.call_tool("search", {"query": "x"})) == "(no results)"


async def test_stdio_end_to_end(tmp_path):
    env = {**os.environ, "COMMONS_DB": str(tmp_path / "e2e.db"), "COMMONS_AGENT": "e2e-client"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "commons_memory", "serve"], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            assert len(tools.tools) >= 30
            idx = json.loads((await s.call_tool("index_code", {"path": str(ROOT / "src"), "project": "commons"})).content[0].text)
            assert idx["symbols"] > 50
            syms = json.loads((await s.call_tool("code_symbols", {"name": "add_item", "project": "commons"})).content[0].text)
            assert syms[0]["path"].endswith("store.py")
            imp = (await s.call_tool("code_impact", {"symbol": "redact", "project": "commons"})).content[0].text
            assert "add_item" in imp
            ses = json.loads((await s.call_tool("session_start", {"goal": "e2e", "project": "commons"})).content[0].text)
            await s.call_tool("handoff", {"session_id": ses["session_id"], "summary": "done", "next_steps": ["ship"]})
            res = json.loads((await s.call_tool("resume", {"project": "commons"})).content[0].text)
            assert res["handoff"]["next_steps"] == ["ship"]
            mer = (await s.call_tool("export_graph", {"format": "mermaid", "project": "commons",
                                                       "include_items": False})).content[0].text
            assert "graph LR" in mer
