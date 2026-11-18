"""An agent picks the model (and effort) another agent runs on, from the models that agent reported it can run."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from commons_memory.cli_v7 import daemon
from commons_memory.orchestrator import Orchestrator
from commons_memory.providers import ProviderLayer
from commons_memory.routing import Router, resolve_model
from commons_memory.store import CommonsError

AGY = [{"id": "gemini-4-hydrogen-high", "name": "Gemini 4 Hydrogen (High)"},
       {"id": "gemini-4-hydrogen-low", "name": "Gemini 4 Hydrogen (Low)"},
       {"id": "claude-opus-5-5-medium", "name": "Claude Opus 5.5 (Medium)"},
       {"id": "gemini-3.8-flash-high", "name": "Gemini 3.8 Flash (High)"}]


def _agy(store, name="antigravity"):
    aid = store.register_agent(name)["id"]  # a teammate with a key, so who(name) finds it
    ProviderLayer(store).sync_agent_models(aid, AGY)
    return aid


def _call(store, tool, args):
    out = asyncio.run(_server(store).call_tool(tool, args))
    return json.loads(out[1]["result"])


_SERVERS = {}


def _server(store):
    from commons_memory.server import build_server
    return _SERVERS.setdefault(id(store), build_server(store))


def test_a_model_is_found_by_id_name_or_a_unique_part(store):
    _agy(store)
    assert resolve_model(store, "antigravity", "claude-opus-5-5-medium") == "antigravity/claude-opus-5-5-medium"
    assert resolve_model(store, "antigravity", "Gemini 3.8 Flash (High)") == "antigravity/gemini-3.8-flash-high"
    assert resolve_model(store, "antigravity", "opus") == "antigravity/claude-opus-5-5-medium"
    assert resolve_model(store, "antigravity", "antigravity/gemini-4-hydrogen-low") == "antigravity/gemini-4-hydrogen-low"
    assert resolve_model(store, "antigravity", "gemini-4-hydrogen", "high") == "antigravity/gemini-4-hydrogen-high"
    with pytest.raises(CommonsError, match="fits several"):
        resolve_model(store, "antigravity", "hydrogen")
    with pytest.raises(CommonsError, match="It can run: .*gemini-3.8-flash-high"):
        resolve_model(store, "antigravity", "gpt-9")
    store.ensure_agent("plain")  # never reported a catalog: taken as given
    assert resolve_model(store, "plain", "whatever-model") == "whatever-model"


def test_ask_and_msg_send_carry_the_chosen_model_and_refuse_one_it_cannot_run(store):
    _agy(store)
    key = store.register_agent("asker")["api_key"]
    r = _call(store, "msg_send", {"to": "antigravity", "body": "hi", "model": "opus", "effort": "high", "api_key": key})
    assert r["model"] == {"model": "antigravity/claude-opus-5-5-medium", "effort": "high"}
    meta = json.loads(store.q("SELECT meta FROM messages ORDER BY id DESC LIMIT 1")[0]["meta"])
    assert meta["model"] == "antigravity/claude-opus-5-5-medium" and meta["effort"] == "high"
    r = _call(store, "ask", {"to": "antigravity", "question": "q?", "model": "flash", "wait": 0, "api_key": key})
    assert r["model"]["model"] == "antigravity/gemini-3.8-flash-high"
    from mcp.server.fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="has no model 'gpt-9'"):
        asyncio.run(_server(store).call_tool("ask", {"to": "antigravity", "question": "q", "model": "gpt-9", "wait": 0,
                                                     "api_key": key}))
    with pytest.raises(ToolError, match="effort must be one of"):
        asyncio.run(_server(store).call_tool("msg_send", {"to": "antigravity", "body": "x", "effort": "huge", "api_key": key}))
    assert "gemini-4-hydrogen-high" in _call(store, "who", {"name": "antigravity", "api_key": key})["models"]


def test_the_woken_agent_starts_on_the_chosen_model(store, tmp_path):
    _agy(store)
    key = store.register_agent("asker")["api_key"]
    _call(store, "msg_send", {"to": "antigravity", "body": "summarise", "model": "opus", "effort": "low", "api_key": key})
    seen = tmp_path / "env.txt"
    sh = tmp_path / "agent.sh"
    sh.write_text(f'#!/bin/sh\ncat > /dev/null; echo "$COMMONS_MODEL $COMMONS_EFFORT" > {seen}; echo done\n')
    sh.chmod(0o755)
    args = SimpleNamespace(agent="antigravity", agent_cmd=str(sh), url=None, api_key=None, wait=0, once=True, timeout=30)
    assert daemon(args, store, log=lambda *_: None) == 0
    assert seen.read_text().split() == ["claude-opus-5-5-medium", "medium"]  # the id carries its own effort


def test_a_handed_off_task_runs_on_the_chosen_model_even_when_another_agent_takes_it(store):
    _agy(store, "antigravity")
    _agy(store, "antigravity-lxc")
    o = Orchestrator(store)
    sender = store.ensure_agent("asker")
    d = o.delegate(sender, "antigravity", "review the release notes, a long brief\n" * 3, model="antigravity/gemini-3.8-flash-high",
                   effort="high")
    st = dict(store.q("SELECT * FROM subtasks WHERE id=?", (d["subtask_id"],))[0])
    agent, dec = Router(store).choose(st, [(1.0, store.get_agent(n)) for n in ("antigravity", "antigravity-lxc")])
    assert dec["model"].endswith("/gemini-3.8-flash-high")  # either Antigravity may take it: the model is what was chosen
    assert dec["effort"]["level"] == "high" and "chosen by the sender" in dec["rationale"]
    assert not d.get("split")  # a chosen model keeps the work whole
