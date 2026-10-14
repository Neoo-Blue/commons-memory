"""Backend actions for the redesigned dashboard: provider key check, retry / reassign of job steps, one-tap answers for
board questions, and the job and agent drawers. Everything goes through the real HTTP server."""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from commons_memory import approvals as AP
from commons_memory import board as B
from commons_memory import onboarding as O
from commons_memory import quota as Q
from commons_memory.dashboard import make_server
from commons_memory.ids import now
from commons_memory.messaging import Messaging
from commons_memory.orchestrator import Orchestrator
from commons_memory.providers import ProviderLayer
from commons_memory.store import CommonsError

from test_orchestration import agents, run_to_end


@pytest.fixture
def hub(store):
    """call(path) is a GET, call(path, body) a POST with the X-Commons header; returns (status, json)."""
    srv = make_server(store, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, body=None, cookie=""):
        req = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST",
                                     headers={"X-Commons": "1", "Content-Type": "application/json", "Cookie": cookie})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    yield call
    srv.shutdown()


def _fail_one_step(store, o, max_attempts=1):
    """A one-step job whose only attempt failed: the step and the job are failed."""
    job = o.submit("Build the importer", dag=[{"key": "only", "type": "code", "title": "Build the importer",
                                               "body": "do it", "max_attempts": max_attempts}])
    sid = job["subtasks"][0]["id"]
    aid = store.q("SELECT assigned_agent FROM subtasks WHERE id=?", (sid,))[0]["assigned_agent"]
    assert o.claim(aid, sid)
    o.complete(sid, aid, "it broke", success=False)
    assert store.q("SELECT status FROM jobs WHERE id=?", (job["job_id"],))[0]["status"] == "failed"
    return job["job_id"], sid, aid


# ---------------------------------------------------------------- provider key check
def test_provider_test_lists_models_and_never_echoes_the_key(store, hub):
    ProviderLayer(store).add_provider("mock", "mock", config={"models": [{"model": "small"}, {"model": "big"}]})
    assert hub("/api/setup/provider_test", {"kind": "mock"}) == (200, {"ok": True, "models": 2, "error": ""})
    code, body = hub("/api/setup/provider_test", {"kind": "openai"})
    assert code == 200 and not body["ok"] and "not set up" in body["error"]

    class Refuse(BaseHTTPRequestHandler):  # a provider that quotes the key back in its error, as some do
        def do_GET(self):
            out = json.dumps({"error": {"message": f"Incorrect API key provided: {self.headers.get('Authorization')}"}})
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out.encode())

        def log_message(self, *a):
            pass
    up = HTTPServer(("127.0.0.1", 0), Refuse)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    try:
        O.set_provider(store, "openai", "opn-Secret77", f"http://127.0.0.1:{up.server_port}")
        code, body = hub("/api/setup/provider_test", {"kind": "openai"})
    finally:
        up.shutdown()
    assert code == 200 and body["ok"] is False and body["models"] == 0 and "HTTP 401" in body["error"], body
    assert "opn-Secret77" not in json.dumps(body)
    assert "opn-Secret77" not in store.q("SELECT last_error FROM providers WHERE name='openai'")[0]["last_error"]
    assert store.q("SELECT 1 FROM audit WHERE action='setup.provider_test'")


# ---------------------------------------------------------------- step actions
def test_retry_a_failed_step_brings_the_job_back(store, hub):
    o = Orchestrator(store)
    ids = agents(store, o)
    jid, sid, who = _fail_one_step(store, o)
    code, body = hub("/api/subtask/retry", {"id": sid})
    assert code == 200 and body["status"] == "pending" and body["max_attempts"] == 2 and body["job_status"] == "running"
    assert body["assigned_to"]  # routed again: the agent that failed it may take it, the owner asked
    assert store.q("SELECT excluded_agents FROM subtasks WHERE id=?", (sid,))[0]["excluded_agents"] == "[]"
    assert store.q("SELECT 1 FROM audit WHERE action='subtask.retry' AND target=?", (sid,))
    run_to_end(o, ids)
    assert o.status(jid)["status"] == "done"
    assert hub("/api/subtask/retry", {"id": sid}) == (400, {"error": "this step is done"})
    code, body = hub("/api/subtask/retry", {"id": "sub_nope"})
    assert code == 400 and "unknown subtask" in body["error"]


def test_retry_job_reruns_failed_steps_and_their_checks(store, hub):
    o = Orchestrator(store)
    ids = agents(store, o)
    job = o.submit("Implement the cache", dag=[
        {"key": "code", "type": "code", "title": "Implement the cache", "body": "x", "gates": ["review"], "max_attempts": 1},
        {"key": "review", "type": "review", "title": "Review the cache", "body": "VERDICT", "deps": ["code"]}])
    run_to_end(o, ids, lambda a, p: ("missing eviction\nVERDICT: reject", "reject") if p["is_gate"] else ("v1", None))
    jid = job["job_id"]
    assert o.status(jid)["status"] == "failed"
    code_id, review_id = (s["id"] for s in job["subtasks"])
    assert hub("/api/job/retry", {"id": "job_nope"})[0] == 400
    code, body = hub("/api/job/retry", {"id": jid})
    assert code == 200 and body["retried"] == [code_id] and body["status"] == "running"
    st = store.q("SELECT status, body FROM subtasks WHERE id=?", (code_id,))[0]
    assert st["status"] == "pending" and "REWORK REQUIRED" in st["body"] and "missing eviction" in st["body"]
    assert dict(store.q("SELECT status, verdict FROM subtasks WHERE id=?", (review_id,))[0]) == \
        {"status": "pending", "verdict": None}  # the check re-runs on the new attempt
    assert store.q("SELECT 1 FROM audit WHERE action='job.retry' AND target=?", (jid,))
    code, body = hub("/api/job/retry", {"id": jid})
    assert code == 400 and "only a failed job" in body["error"]
    run_to_end(o, ids)
    assert o.status(jid)["status"] == "done"


def test_reassign_moves_a_claimed_step_and_pins_it(store, hub):
    o = Orchestrator(store)
    ids = agents(store, o)
    job = o.submit("Build the exporter", dag=[
        {"key": "code", "type": "code", "title": "Build the exporter", "body": "x", "gates": ["review"]},
        {"key": "review", "type": "review", "title": "Review the exporter", "body": "VERDICT", "deps": ["code"]}])
    code_id, review_id = (s["id"] for s in job["subtasks"])
    holder = store.q("SELECT assigned_agent FROM subtasks WHERE id=?", (code_id,))[0]["assigned_agent"]
    assert o.claim(holder, code_id)
    target = next(n for n, a in ids.items() if a != holder)
    code, body = hub("/api/subtask/reassign", {"id": code_id, "agent": target})
    assert code == 200 and body["status"] == "pending" and body["assigned_to"] == target and body["pinned"], body
    assert store.q("SELECT value FROM meta WHERE key=?", (f"pin:{code_id}",))[0]["value"] == ids[target]
    assert store.q("SELECT 1 FROM wake_events WHERE agent_id=? AND event='lease_expired' AND ref=?", (holder, code_id))
    assert [r["value"] for r in store.q("SELECT value FROM meta WHERE key LIKE ?", (f"woke:{code_id}:%",))] == [ids[target]]
    with pytest.raises(CommonsError):
        o.complete(code_id, holder, "late result")  # the old holder lost it
    assert store.q("SELECT 1 FROM audit WHERE action='subtask.reassign' AND target=?", (code_id,))
    code, body = hub("/api/subtask/reassign", {"id": review_id, "agent": target})
    assert code == 400 and "did the work this step checks" in body["error"]
    code, body = hub("/api/subtask/reassign", {"id": code_id, "agent": "nobody"})
    assert code == 400 and "unknown agent" in body["error"]
    assert hub("/api/subtask/reassign", {"id": "sub_nope", "agent": target})[0] == 400
    p = o.claim(ids[target], code_id)
    o.complete(p["subtask_id"], ids[target], "exporter done")
    code, body = hub("/api/subtask/reassign", {"id": code_id, "agent": target})
    assert code == 400 and "only pending or claimed" in body["error"]


def test_step_actions_need_the_owner_once_a_password_exists(store, hub):
    ProviderLayer(store).add_provider("mock", "mock", config={"models": [{"model": "small"}]})
    AP.set_pin(store, "123456", by="host")
    for path, b in (("/api/subtask/retry", {"id": "sub_x"}), ("/api/subtask/reassign", {"id": "sub_x", "agent": "a"}),
                    ("/api/job/retry", {"id": "job_x"}), ("/api/setup/provider_test", {"kind": "mock"})):
        code, body = hub(path, b)
        assert code == 403 and body["need_pin"], path
    code, body = hub("/approve/unlock", {"pin": "123456"})
    assert code == 200
    cookie = f"commons_approver={AP.session_token(store)}"
    assert hub("/api/setup/provider_test", {"kind": "mock"}, cookie) == (200, {"ok": True, "models": 1, "error": ""})
    assert hub("/api/subtask/retry", {"id": "sub_x"}, cookie)[0] == 400  # past the gate: an unknown step


# ---------------------------------------------------------------- one-tap answers
@pytest.mark.parametrize("text,want", [
    ("Postgres or SQLite?", ["Postgres", "SQLite"]),
    ("Should we use Postgres or SQLite?", ["Postgres", "SQLite"]),
    ("Which theme: dark mode or light mode?", ["Dark mode", "Light mode"]),
    ("Should we use React, Vue, or Svelte?", ["React", "Vue", "Svelte"]),
    ("Red, green or blue?", ["Red", "Green", "Blue"]),
    ("If it fails, should we retry or skip?", ["Retry", "Skip"]),
    ("Ship today or wait until Monday?", ["Ship today", "Wait until Monday"]),
    ("Should we upgrade to v1.2 or stay on v1.1?", ["Upgrade to v1.2", "Stay on v1.1"]),
    ("Should we go to Paris or to London?", ["Paris", "London"]),
    ("Should we ship it or not?", ["Yes", "No"]),
    ("The build is green. Should I deploy to production?", ["Yes", "No"]),
    ("Can I delete the old branch?", ["Yes", "No"]),
    ("Do you want me to open a PR?", ["Yes", "No"]),
    ("Is it ok to restart Plex now?", ["Yes", "No"]),
    ("What should the new name be?", []),
    ("Please review the plan.", []),
    ("A, B, C, D, or E?", []),  # more than four: no partial list
    ("Is it ok to deploy now or should I wait?", []),  # reads both ways
    ("Should we rewrite the ingestion pipeline in Rust or keep patching the Python version we have today?", []),
    ("[vault svc] Allow DELETE on the media server or not?", []),
    ("[mcp github] Allow create_issue?", []),
    ("[gate sub_1] Approve the spec? Answer yes, or no with what to change.", []),
    ("[rework sub_1] Two more rounds or stop?", []),
    ("", []),
])
def test_choices(text, want):
    assert B.choices(text) == want


def test_open_questions_carry_choices_on_board_and_overview(store, hub):
    q = B.add(store, "question", "Should we use Postgres or SQLite?", project="site")
    v = B.add(store, "question", "[vault svc] Allow DELETE on the media server?", project="site")
    old = B.add(store, "question", "Can I restart the box?", project="site")
    B.answer(store, old["id"], "yes")
    B.add(store, "ask", "make the site faster", project="site")
    code, board = hub("/api/board")
    by = {x["id"]: x for x in board["items"]}
    assert code == 200 and by[q["id"]]["choices"] == ["Postgres", "SQLite"] and by[v["id"]]["choices"] == []
    assert "choices" not in by[old["id"]] and all("choices" not in x for x in board["items"] if x["kind"] == "ask")
    code, ov = hub("/api/overview")
    assert code == 200 and [(x["id"], x["choices"]) for x in ov["needs_user"]] == \
        [(q["id"], ["Postgres", "SQLite"]), (v["id"], [])]
    assert B.needs_user(store, "site")[0]["choices"] == ["Postgres", "SQLite"]


# ---------------------------------------------------------------- drawers
def test_job_detail_has_steps_routing_and_workspace(store, hub):
    o = Orchestrator(store)
    ids = agents(store, o)
    job = o.submit("Implement rate limiter", pattern="cross_review", project="api", agent_id=ids["claude"])
    code_id = job["subtasks"][0]["id"]
    aid = store.q("SELECT assigned_agent FROM subtasks WHERE id=?", (code_id,))[0]["assigned_agent"]
    o.complete(o.claim(aid, code_id)["subtask_id"], aid, "limiter " * 100)
    code, d = hub(f"/api/job_detail?id={job['job_id']}")
    assert code == 200 and d["job"]["id"] == job["job_id"] and d["job"]["project"] == "api"
    assert d["job"]["submitted_by"] == "claude" and d["job"]["pattern"] == "cross_review" and "project_id" not in d["job"]
    assert d["folder"] == f"/jobs/{job['job_id']}" and "workflow" not in d
    st = {s["key"]: s for s in d["subtasks"]}
    assert set(st) == {"code", "review", "red_team"}
    c = st["code"]
    assert {"id", "key", "type", "title", "status", "agent", "attempts", "verdict", "result", "created_at", "updated_at",
            "gate_of", "deps", "route"} <= set(c)
    assert c["status"] == "awaiting_gate" and c["attempts"] == 1 and len(c["result"]) <= 301 and c["result"].endswith("…")
    assert c["created_at"].startswith("20") and st["review"]["gate_of"] == code_id and st["review"]["deps"] == [code_id]
    assert c["route"] and {"model", "effort", "rationale"} == set(c["route"]) and c["route"]["effort"]
    code, body = hub("/api/job_detail?id=job_nope")
    assert code == 400 and "unknown job" in body["error"]
    wf = o.submit("Add CSV export to the reports page", pattern="sdlc")
    code, d = hub(f"/api/job_detail?id={wf['job_id']}")
    assert code == 200 and d["workflow"]["job"]["id"] == wf["job_id"] and d["workflow"]["stages"]


def test_agent_detail_has_presence_usage_and_recent_steps(store, hub):
    o = Orchestrator(store)
    ids = agents(store, o)
    a = ids["claude"]
    job = o.submit("Port the parser", dag=[{"key": "only", "type": "code", "title": "Port the parser", "body": "x",
                                            "agent": "claude"}])
    store.x("INSERT INTO agent_models VALUES(?,?)", (a, "mock/big"))
    Q.report(store, a, [{"window": "five_hour", "used_pct": 40, "resets_at": now() + 3600}])
    store.x("INSERT INTO meta VALUES(?,?)", (f"agent_cli:{a}", json.dumps({"version": "1.2.3", "updated_at": now()})))
    Messaging(store).send(None, "claude", "ping")
    code, d = hub("/api/agent_detail?name=claude")
    assert code == 200 and d["name"] == "claude" and d["presence"]["agent"] == "claude"
    assert d["profile"] == {"expertise": {"code": 0.9, "review": 0.6}, "cost": 3, "speed": 3, "max_concurrent": 3}
    assert d["models"] == ["mock/big"] and d["cli"]["version"] == "1.2.3"
    assert d["usage"]["can_take_work"] and d["usage"]["windows"][0]["window"] == "five_hour"
    assert d["usage"]["windows"][0]["used_pct"] == 40
    assert d["recent"][0] | {"updated_at": None} == {"subtask_id": job["subtasks"][0]["id"], "job_id": job["job_id"],
                                                      "job": "Port the parser", "title": "Port the parser",
                                                      "status": "pending", "updated_at": None}
    assert d["unread"] >= 1
    code, body = hub("/api/agent_detail?name=nobody")
    assert code == 400 and "unknown agent" in body["error"]


def test_usage_series_splits_daily_tokens_by_agent(store):
    from commons_memory.dashboard import API
    from commons_memory.usage import Usage
    a, b = store.ensure_agent("alpha"), store.ensure_agent("beta")
    Usage(store).report(a, tokens_in=100, tokens_out=50, cost=0.1)
    Usage(store).report(b, tokens_in=10, tokens_out=5, cost=0.01)
    d = API(store).usage_series({"days": "3"})
    assert sum(d["token_series"]["alpha"]) == 150 and sum(d["token_series"]["beta"]) == 15 and sum(d["tokens"]) == 165
