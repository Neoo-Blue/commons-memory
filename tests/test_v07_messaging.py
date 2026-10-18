import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from commons_memory.messaging import (Channels, Dispatcher, Messaging, ReplayGuard, board, check_url, emit, heartbeat,
                                      presence)
from commons_memory.store import CommonsError


class Hook(BaseHTTPRequestHandler):
    got: list = []
    code = 200

    def log_message(self, *a):
        pass

    def do_POST(self):
        b = self.rfile.read(int(self.headers["Content-Length"]))
        Hook.got.append((dict(self.headers), b))
        self.send_response(Hook.code)
        self.end_headers()


@pytest.fixture
def hook(monkeypatch):
    Hook.got, Hook.code = [], 200
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("COMMONS_WEBHOOK_ALLOWLIST", "127.0.0.1")
    monkeypatch.setenv("COMMONS_WAKE_SYNC", "1")
    yield f"http://127.0.0.1:{srv.server_address[1]}/hook"
    srv.shutdown()


def test_messaging_direct_broadcast_ack_lease_threads(store):
    m = Messaging(store)
    a, b, c = (store.ensure_agent(n) for n in ("a", "b", "c"))
    r = m.send(a, "b", "please review PR 4", mtype="review", subject="PR 4")
    m.send(b, "a", "on it", thread=r["thread"], reply_to=r["id"])
    m.send(a, "*", "deploy at 5")
    got = m.recv("b", lease=30)
    assert {x["text"] for x in got} == {"please review PR 4", "deploy at 5"}
    assert m.recv("b", lease=30) == []  # leased: hidden until lease expires or acked
    m.ack("b", [x["id"] for x in got])
    assert m.unread(b) == 0
    assert [x["text"] for x in m.recv("c")] == ["deploy at 5"]
    th = m.history(a, thread=r["thread"])
    assert [x["text"] for x in th] == ["please review PR 4", "on it"]
    with pytest.raises(CommonsError):
        m.send(a, "b", "x", mtype="bogus")


def test_long_poll_wakes_on_send(store):
    m = Messaging(store)
    a = store.ensure_agent("a")
    store.ensure_agent("b")
    threading.Timer(0.3, lambda: m.send(a, "b", "late")).start()
    t = time.time()
    got = m.recv("b", wait=5)
    assert got and got[0]["text"] == "late" and time.time() - t < 4


def test_webhook_wake_signed_replay_and_dead_letter(store, hook, monkeypatch):
    b = store.ensure_agent("b")
    ch = Channels(store).add("b", "webhook", {"url": hook})
    Messaging(store).send(store.ensure_agent("a"), "b", "secret is sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD now")
    Dispatcher.run_due(store)
    assert Hook.got, "webhook not called"
    h, body = Hook.got[-1]
    p = json.loads(body)
    assert p["event"] in ("message", "task") and "sk-abc" not in body.decode()
    g = ReplayGuard(ch["secret"])
    assert g.verify(h, body) and not g.verify(h, body)  # replay rejected
    assert not ReplayGuard("wrong").verify(h, body)
    # failures -> retry with backoff -> dead letter -> redrive
    Hook.code = 500
    monkeypatch.setenv("COMMONS_WAKE_BACKOFF_SCALE", "0")
    emit(store, b, "escalation", preview="stuck")
    for _ in range(12):
        Dispatcher.run_due(store)
    assert Dispatcher.dead_letters(store)
    Hook.code = 200
    assert Dispatcher.redrive(store) >= 1
    Dispatcher.run_due(store)
    assert not Dispatcher.dead_letters(store)


def test_ssrf_protection(monkeypatch):
    monkeypatch.delenv("COMMONS_WEBHOOK_ALLOWLIST", raising=False)
    for bad in ("http://example.com/x", "https://127.0.0.1/x", "https://169.254.169.254/latest", "https://10.0.0.5/",
                "file:///etc/passwd", "https://localhost/x"):
        with pytest.raises(CommonsError):
            check_url(bad)
    monkeypatch.setenv("COMMONS_WEBHOOK_ALLOWLIST", "hooks.example.com")
    with pytest.raises(CommonsError, match="ALLOWLIST"):
        check_url("https://evil.example.net/")


def test_channels_validate_and_command_local_only(store):
    with pytest.raises(CommonsError, match="local owner"):
        Channels(store).add("x", "command", {"cmd": "rm -rf /"})
    with pytest.raises(CommonsError, match="secret reference"):
        Channels(store).add("x", "pushover", {"user_ref": "plainvalue", "token_ref": "env://T"})
    assert Channels(store).add("x", "command", {"cmd": "echo hi"}, local_owner=True)["kind"] == "command"


def test_presence_states(store):
    a = store.ensure_agent("p1")
    assert presence(store, a)["state"] == "offline"
    heartbeat(store, a, status="online")
    assert presence(store, a)["state"] == "online"
    heartbeat(store, a, sleeping=True)
    assert presence(store, a)["state"] == "sleeping"
    assert any(x["agent"] == "p1" for x in board(store))
    from commons_memory.messaging import Channels, touch
    store.x("UPDATE agent_presence SET sleeping=0, last_heartbeat=? WHERE agent_id=?", (time.time() - 2 * 86400, a))
    assert presence(store, a)["state"] == "away"                      # checks in on its own, can't be woken
    Channels(store).add("p1", "ntfy", {"topic": "p1-wake"})
    assert presence(store, a)["state"] == "idle"                      # Commons can wake it: available
    b = store.ensure_agent("p2")
    store.x("UPDATE agent_presence SET last_heartbeat=? WHERE agent_id=?", (time.time() - 30 * 86400, b))
    touch(store, b)                                                   # a call with its own key
    assert presence(store, b)["state"] == "online"


def _client(store):
    from starlette.testclient import TestClient

    from commons_memory.agent_http import build_app
    from commons_memory.auth import AuthMiddleware
    return TestClient(AuthMiddleware(build_app(store), store, resource_url="http://testserver", allow_anon_reads=False))


def test_bus_shim_and_native_http(store):
    ka = store.register_agent("grok")["api_key"]
    kb = store.register_agent("muse")["api_key"]
    c = _client(store)
    assert c.get("/bus/recv").status_code == 401
    r = c.post("/bus/send", json={"from": "grok", "to": "muse", "text": "hello via bus"}, headers={"X-Bridge-Token": ka})
    assert r.status_code == 200, r.text
    assert c.post("/bus/send", json={"from": "muse", "to": "grok", "text": "spoof"}, headers={"X-Bridge-Token": ka}).status_code == 403
    got = c.get("/bus/recv?for=muse", headers={"X-Bridge-Token": kb}).json()["messages"]
    assert got[0]["text"] == "hello via bus" and got[0]["from"] == "grok"
    assert c.post("/bus/ack", json={"for": "muse", "ids": [got[0]["id"]]}, headers={"X-Bridge-Token": kb}).json()["ok"]
    assert c.get("/bus/recv?for=muse", headers={"X-Bridge-Token": kb}).json()["messages"] == []
    assert c.get("/bus/history?for=grok", headers={"X-Bridge-Token": ka}).json()["history"]
    h = {"Authorization": f"Bearer {kb}"}
    assert c.post("/agent/heartbeat", json={"status": "online"}, headers=h).json()["state"] == "online"
    assert c.get("/agent/presence", headers=h).json()["agents"]
    with c.websocket_connect("/agent/ws", headers=h) as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"op": "send", "to": "grok", "text": "over ws"})
        assert ws.receive_json()["type"] == "sent"
    assert c.get("/agent/recv", headers={"Authorization": f"Bearer {ka}"}).json()["messages"][0]["text"] == "over ws"


def test_sse_stream_receives_wake(store):
    from commons_memory.agent_http import build_app
    from commons_memory.auth import PRINCIPAL
    k = store.register_agent("listener")
    aid = store.get_agent("listener")["id"]
    app = build_app(store)
    events = []

    async def run():
        tok = PRINCIPAL.set(aid)
        sent = []
        disconnect = asyncio.Event()

        async def receive():
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(msg):
            if msg["type"] == "http.response.body" and msg.get("body"):
                events.append(msg["body"].decode())
                if len(events) == 1:
                    await asyncio.to_thread(Messaging(store).send, store.ensure_agent("x"), "listener", "ping")
                elif any("event: wake" in e for e in events):
                    disconnect.set()
                    raise asyncio.CancelledError
        scope = {"type": "http", "method": "GET", "path": "/agent/stream", "headers": [], "query_string": b""}
        try:
            await asyncio.wait_for(app(scope, receive, send), 10)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        PRINCIPAL.reset(tok)
        return sent
    asyncio.run(run())
    assert k["api_key"] and events[0].startswith("event: hello") and any("event: wake" in e for e in events)


def test_bus_import_and_daemon(store, tmp_path):
    from commons_memory.cli_v7 import bus_import, daemon

    class Bus(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            assert self.headers["X-Bridge-Token"] == "tok"
            rows = [{"id": 1, "from": "grok", "to": "muse", "text": "old one", "ts": "2026-10-01T10:00:00Z"},
                    {"id": 2, "from": "muse", "to": "grok", "text": "reply", "ts": 1759312800}]
            body = json.dumps({"ok": True, "messages": rows if "before" not in self.path else []}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Bus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    assert bus_import(store, url, "tok")["imported"] == 2
    assert bus_import(store, url, "tok")["imported"] == 0  # idempotent
    srv.shutdown()
    m = Messaging(store)
    assert {x["text"] for x in m.history(peer=None, limit=10)} >= {"old one", "reply"}
    # daemon wakes a local CLI and acks on success
    m.send(store.ensure_agent("boss"), "worker1", "do the thing", mtype="task")
    out = tmp_path / "prompt.txt"
    args = SimpleNamespace(agent="worker1", agent_cmd=f"tee {out}", url=None, api_key=None, wait=0, once=True, timeout=30)
    assert daemon(args, store, log=lambda *_: None) == 0
    prompt = out.read_text()
    assert "from boss" in prompt and "msg_get(" in prompt
    assert "do the thing" not in prompt          # the body is read with the agent's own key, as data, not pasted in
    assert m.unread(store.get_agent("worker1")["id"]) == 0


def test_routing_records_target_effort_and_escalates(store):
    from commons_memory.orchestrator import Orchestrator
    from commons_memory.routing import Router, cli_target, effort_spec, provider_params
    o = Orchestrator(store)
    a = store.ensure_agent("coder")
    o.profiles.set(a, expertise={"code": 0.9, "plan": 0.8}, max_concurrent=4, models=["claude-sonnet-4-5", "claude-opus-4-1"]) \
        if "models" in o.profiles.set.__code__.co_varnames else o.profiles.set(a, expertise={"code": 0.9}, max_concurrent=4)
    jid = o.submit("implement the parser", dag=[{"key": "x", "type": "code", "title": "implement parser"}], project="r")["job_id"]
    p = o.claim(a)
    w = Router(store).why(p["subtask_id"])
    assert w.get("effort") or w.get("level") or w.get("decisions"), w
    o.complete(p["subtask_id"], a, "done")
    assert Router(store).decisions(5)
    eff = effort_spec("high")
    assert provider_params("openai", "o3", eff) == {"reasoning_effort": "high"}
    assert provider_params("anthropic", "claude-sonnet-4-5", eff)["thinking"]["budget_tokens"] == eff["thinking_budget"]
    assert "thinkingBudget" in json.dumps(provider_params("google", "gemini-2.5-pro", effort_spec("minimal")))
    cmd, env = cli_target("codex exec --full-auto", "openai/gpt-5-codex", eff)
    assert cmd.startswith("codex exec -m gpt-5-codex -c model_reasoning_effort=high")
    cmd, env = cli_target("claude -p", "claude-opus-4-1", eff)
    assert "--model claude-opus-4-1" in cmd and env["MAX_THINKING_TOKENS"]
    assert jid


def test_friendly_tool_args_and_project_names(store):
    from mcp.server.fastmcp.exceptions import ToolError

    from commons_memory.server import build_server
    srv = build_server(store)

    async def call(name, args):
        return await srv._tool_manager.call_tool(name, args)
    r = asyncio.run(call("remember", {"text": "aliases work", "projectName": "alias-proj"}))
    assert "mem_" in str(r)
    asyncio.run(call("record_decision", {"decision": "use sqlite", "rationale": "simple"}))
    with pytest.raises(ToolError, match="unknown argument 'bogus'.*Accepted"):
        asyncio.run(call("remember", {"content": "x", "bogus": 1}))
    with pytest.raises(ToolError, match="needs 'body'|needs 'to'"):
        asyncio.run(call("msg_send", {"agent": "a"}))
    tool = srv._tool_manager.get_tool("remember")
    assert "Aliases:" in tool.description
    with pytest.raises(CommonsError, match="invalid project name"):
        store.project_id("commons-live?")
    assert store.project_id("commons-live")


def test_an_agent_out_of_usage_still_answers_whoever_asked(store, tmp_path):
    from types import SimpleNamespace

    from commons_memory.cli_v7 import daemon
    m = Messaging(store)
    m.send(store.ensure_agent("grok"), "worker2", "Roll call: who are you?", subject="Roll call")
    fail = tmp_path / "agent.sh"
    fail.write_text("#!/bin/sh\necho 'Error: 402 Payment Required: weekly limit reached' >&2\nexit 1\n")
    fail.chmod(0o755)
    args = SimpleNamespace(agent="worker2", agent_cmd=str(fail), url=None, api_key=None, wait=0, once=True, timeout=30)
    daemon(args, store, log=lambda *_: None)
    reply = m.recv(store.get_agent("grok")["id"])
    assert reply and "out of usage" in reply[0]["text"] and reply[0]["from"] == "worker2"


def test_unread_mail_never_piles_up(store):
    m = Messaging(store)
    a = store.ensure_agent("sender3")
    old = m.send(a, "quiet-agent", "a week-old note")["id"]
    store.x("UPDATE messages SET created_at=? WHERE id=?", (time.time() - 7 * 86400, old))
    m.send(a, "quiet-agent", "fresh")
    q = store.get_agent("quiet-agent")["id"]
    assert m.unread(q) == 1 and [x["text"] for x in m.recv(q)] == ["fresh"]
    assert any(x["text"] == "a week-old note" for x in m.history(q))       # expired, not deleted
