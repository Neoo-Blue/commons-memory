"""Agents claim their work, talk like a chat, and check that old work is still wanted (review of 2026-11-06)."""
import threading
import time
from types import SimpleNamespace

from commons_memory.cli_v7 import daemon
from commons_memory.messaging import Messaging, heartbeat, still_wanted
from commons_memory.orchestrator import Orchestrator


def _script(tmp_path, name, body):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return str(p)


def _args(agent, cmd):
    return SimpleNamespace(agent=agent, agent_cmd=cmd, url=None, api_key=None, wait=0, once=True, timeout=30)


def test_the_daemon_claims_the_work_before_the_agent_starts(store, tmp_path):
    o = Orchestrator(store)
    j = o.submit("write the release note", dag=[{"key": "a", "type": "write", "title": "Release note", "body": "x"}])
    sid = o.status(j["job_id"])["subtasks"][0]["id"]
    w = store.ensure_agent("worker")
    store.x("UPDATE subtasks SET assigned_agent=? WHERE id=?", (w, sid))
    Messaging(store).send(None, "worker", "task ready", mtype="task", meta={"subtask": sid, "job": j["job_id"]})
    seen = tmp_path / "prompt.txt"
    cmd = _script(tmp_path, "agent.sh", f"cat > {seen}; echo 'I need the owner to sign in for this.'\n")
    assert daemon(_args("worker", cmd), store, log=lambda *_: None) == 0
    prompt = seen.read_text()
    assert f"Commons has already claimed {sid}" in prompt and "WORK PACKET" in prompt   # claimed before it started
    st = store.q("SELECT status FROM subtasks WHERE id=?", (sid,))[0]
    assert st["status"] not in ("claimed", "done")  # no result handed in: back to the hub, never "done" on our say-so


def test_the_asker_gets_an_answer_even_if_the_agent_forgets_to_send_it(store, tmp_path):
    m = Messaging(store)
    asker = store.ensure_agent("asker")
    q = m.send(asker, "answerer", "what is the port?", mtype="question")["id"]
    cmd = _script(tmp_path, "agent.sh", "cat > /dev/null; echo 'It is 8770.'\n")
    assert daemon(_args("answerer", cmd), store, log=lambda *_: None) == 0
    got = m.recv(asker)
    assert got and got[0]["text"] == "It is 8770." and got[0]["reply_to"] == q


def test_a_failing_cli_gives_up_after_two_tries_and_says_so(store, tmp_path):
    m = Messaging(store)
    asker = store.ensure_agent("asker2")
    m.send(asker, "broken", "please look at this", mtype="question")
    cmd = _script(tmp_path, "agent.sh", "cat > /dev/null; echo 'error: bad flag --effort' >&2; exit 1\n")
    import commons_memory.cli_v7 as C
    sleep, C.time.sleep = C.time.sleep, lambda *_: None  # skip the 30 s / 120 s retry pauses
    try:
        daemon(_args("broken", cmd), store, log=lambda *_: None)
        b = store.get_agent("broken")["id"]
        assert m.unread(b) == 1                                    # first failure: kept for one more try
        store.x("UPDATE message_deliveries SET visible_at=0 WHERE agent_id=?", (b,))
        daemon(_args("broken", cmd), store, log=lambda *_: None)
    finally:
        C.time.sleep = sleep
    assert m.unread(b) == 0                                        # second failure: dropped, not woken forever
    told = m.recv(asker)
    assert told and "failed twice" in told[0]["text"] and "bad flag" in told[0]["text"]


def test_ask_returns_the_answer_in_the_same_call(store):
    m = Messaging(store)
    a, b = store.ensure_agent("alice"), store.ensure_agent("bob")
    heartbeat(store, b, client="daemon")  # bob has a wake service: he can be reached like a chat

    def bob():
        for _ in range(50):
            got = m.recv(b, client="daemon")  # as a wake service polls
            if got:
                m.send(b, "alice", "blue", reply_to=got[0]["id"])
                return
            time.sleep(0.05)
    threading.Thread(target=bob, daemon=True).start()
    r = m.ask(a, "bob", "which colour?", wait=10)
    assert r["status"] == "answered" and r["answer"] == "blue" and r["seconds"] < 10
    assert m.unread(a) == 0                                        # read in the call, not handed over again


def test_ask_does_not_hang_on_an_agent_that_cannot_be_woken(store):
    m = Messaging(store)
    a = store.ensure_agent("carol")
    store.ensure_agent("sleepy-bot")
    t = time.time()
    r = m.ask(a, "sleepy-bot", "are you there?", wait=30)
    assert r["status"] == "queued" and time.time() - t < 2 and m.unread(store.get_agent("sleepy-bot")["id"]) == 1


def _old_job(store, poster):
    o = Orchestrator(store)
    j = o.submit("map the affiliate programs", agent_id=poster,
                 dag=[{"key": "a", "type": "research", "title": "Map affiliate programs", "body": "x"}])
    store.x("UPDATE jobs SET created_at=? WHERE id=?", (time.time() - 3 * 86400, j["job_id"]))
    return o, j["job_id"]


def _poster_replies(store, poster, text):
    m = Messaging(store)

    def run():
        for _ in range(100):
            got = [x for x in m.recv(poster, client="daemon") if x["type"] == "question"]
            if got:
                m.send(poster, got[0]["from"], text, reply_to=got[0]["id"])
                return
            time.sleep(0.05)
    threading.Thread(target=run, daemon=True).start()


def test_old_work_asks_its_poster_first_and_a_no_cancels_it(store, monkeypatch):
    monkeypatch.setenv("COMMONS_STILL_NEEDED_WAIT", "10")
    poster, worker = store.ensure_agent("planner"), store.ensure_agent("doer")
    heartbeat(store, poster, client="daemon")
    o, jid = _old_job(store, poster)
    _poster_replies(store, poster, "No, already done last week.")
    assert o.claim(worker) is None
    assert store.q("SELECT status FROM jobs WHERE id=?", (jid,))[0]["status"] == "cancelled"


def test_old_work_goes_ahead_when_its_poster_says_yes(store, monkeypatch):
    monkeypatch.setenv("COMMONS_STILL_NEEDED_WAIT", "10")
    poster, worker = store.ensure_agent("planner2"), store.ensure_agent("doer2")
    heartbeat(store, poster, client="daemon")
    o, jid = _old_job(store, poster)
    _poster_replies(store, poster, "Yes, still needed.")
    p = o.claim(worker)
    assert p and store.q("SELECT status FROM jobs WHERE id=?", (jid,))[0]["status"] == "running"
    # a poster that can't be woken doesn't hold the work up
    poster3, worker3 = store.ensure_agent("planner3"), store.ensure_agent("doer3")
    o3, _ = _old_job(store, poster3)
    t = time.time()
    assert o3.claim(worker3) and time.time() - t < 3


def test_reading_a_still_needed_answer():
    assert still_wanted("Yes, go ahead") is True
    assert still_wanted("no.") is False and still_wanted("Nope") is False
    assert still_wanted("That one is already done, thanks") is False
    assert still_wanted("No problem, carry on") is not False
    assert still_wanted("Yes, though step 2 is not needed") is True   # the direct answer wins


def test_broadcasts_skip_the_hubs_own_names(store):
    m = Messaging(store)
    for n in ("librarian", "provider:anthropic", "dashboard-user"):
        store.ensure_agent(n)
    real = store.ensure_agent("teammate")
    r = m.send(store.ensure_agent("boss2"), "*", "standup in 5")
    names = {x["agent_id"] for x in store.q("SELECT agent_id FROM message_deliveries WHERE msg_id=?", (r["id"],))}
    assert real in names and not names & {store.get_agent(n)["id"] for n in ("librarian", "provider:anthropic")}


def test_jev_skips_chat_captures_and_drops_their_old_conflicts(tmp_path, monkeypatch):
    from conftest import make_store

    from commons_memory import typesafe
    from commons_memory.librarian import Librarian
    s = make_store(tmp_path)
    a = s.add_item("memory", "Review still running, pushing after it.", project="ops", tags=["conversation", "assistant"])["id"]
    b = s.add_item("memory", "Review done, everything pushed.", project="ops", tags=["conversation", "assistant"])["id"]
    s.x("INSERT INTO conflicts VALUES('cfl_old', ?, ?, 'text', 'contradicts', 'open', 0)", (a, b))
    s.x("UPDATE items SET conflict=1 WHERE id IN (?,?)", (a, b))
    calls = []
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(typesafe, "noul", lambda state, qs, timeout=20: calls.append(state) or {k: 0.99 for k in qs})
    Librarian(s).run(full=True)
    assert not calls and not s.list_conflicts()
    assert s.q("SELECT conflict FROM items WHERE id=?", (a,))[0]["conflict"] == 0


def test_a_session_claims_its_task_from_the_first_message_and_closes_it_at_the_end(store):
    from commons_memory.capture import Capture
    c = Capture(store)
    a = store.ensure_agent("sessioner")
    r = c.event(a, "prompt", {"prompt": "fix the login redirect bug on the dashboard", "session_id": "s1",
                              "cwd": "/tmp/app"}, harness="claude-code")
    assert "opened work claim wrk_" in r["context"]
    claim = store.q("SELECT id, status FROM work_claims WHERE agent_id=?", (a,))
    assert len(claim) == 1 and claim[0]["status"] == "active"
    c.event(a, "prompt", {"prompt": "also check the logout path please", "session_id": "s1"}, harness="claude-code")
    c.event(a, "prompt", {"prompt": "ok", "session_id": "s2"}, harness="claude-code")  # too short to be a task
    c.event(a, "prompt", {"prompt": "CANON (current truth; precedence: 1 the owner's words) ... then the task", "session_id": "s2b"},
            harness="claude-code")  # injected text is not a task title
    assert len(store.q("SELECT 1 FROM work_claims WHERE agent_id=?", (a,))) == 1         # one claim per session
    from commons_memory import typesafe
    import os
    os.environ["TYPESAFE_API_KEY"] = "test"
    typesafe_noul = typesafe.noul
    typesafe.noul = lambda state, qs, timeout=20, key=None: {"done": 0.1 if "couldn't" in state else 0.9}
    try:
        c.event(a, "session_end", {"session_id": "s1", "replies": ["Fixed: the redirect keeps next= now."]},
                harness="claude-code")
        row = store.q("SELECT status, result FROM work_claims WHERE id=?", (claim[0]["id"],))[0]
        assert row["status"] == "done" and "redirect" in row["result"]
        c.event(a, "prompt", {"prompt": "rotate the staging database password", "session_id": "s3"}, harness="claude-code")
        c.event(a, "session_end", {"session_id": "s3", "replies": ["I couldn't: that needs the owner's console login."]},
                harness="claude-code")
        last = store.q("SELECT status FROM work_claims WHERE agent_id=? ORDER BY started_at DESC LIMIT 1", (a,))[0]
        assert last["status"] == "dropped"  # never shows up later as DONE BEFORE
    finally:
        typesafe.noul = typesafe_noul
        os.environ.pop("TYPESAFE_API_KEY", None)


def test_dashboard_tokens_are_optional(store, monkeypatch):
    """Tokens stay on by default once a dashboard user exists; COMMONS_DASHBOARD_TOKENS=0 turns them off on a hub
    (the owner's NAS), leaving the private-address, Host and owner-password checks in place."""
    import urllib.error
    import urllib.request

    from commons_memory.dashboard import add_dashboard_user, make_server
    add_dashboard_user(store, "owner", "admin")

    def status(base):
        try:
            with urllib.request.urlopen(base + "/", timeout=5) as r:  # the page itself
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
    for env, want in ((None, 401), ("0", 200)):
        if env is None:
            monkeypatch.delenv("COMMONS_DASHBOARD_TOKENS", raising=False)
        else:
            monkeypatch.setenv("COMMONS_DASHBOARD_TOKENS", env)
        srv = make_server(store, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            assert status(f"http://127.0.0.1:{srv.server_address[1]}") == want
        finally:
            srv.shutdown()
    with __import__("pytest").raises(Exception):  # tokens off never means a public, tokenless bind
        make_server(store, "0.0.0.0", 0)


def test_a_moved_step_is_mailed_to_its_new_agent(store):
    o = Orchestrator(store)
    j = o.submit("check the camera feeds", dag=[{"key": "a", "type": "research", "title": "Check feeds", "body": "x"}])
    sid = o.status(j["job_id"])["subtasks"][0]["id"]
    old, new = store.ensure_agent("quiet-bot"), store.ensure_agent("busy-bot")
    store.x("UPDATE subtasks SET assigned_agent=?, escalation='notified' WHERE id=?", (old, sid))
    store.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"woke:{sid}:0:0", old))  # mailed once, long ago
    o.reassign_step(sid, "busy-bot")
    mail = Messaging(store).recv(new)
    assert mail and mail[0]["meta"].get("subtask") == sid
    assert store.q("SELECT escalation FROM subtasks WHERE id=?", (sid,))[0]["escalation"] is None


def test_a_recurring_task_can_be_switched_off_and_on(store):
    o = Orchestrator(store)
    o.schedule("weekly-readme-audit", "@weekly", "audit the README")
    assert o.unschedule("weekly-readme-audit") == {"recurring": "weekly-readme-audit", "enabled": False}
    assert store.q("SELECT enabled FROM recurring")[0]["enabled"] == 0
    o.schedule("weekly-readme-audit", "@weekly", "audit the README")
    assert store.q("SELECT enabled FROM recurring")[0]["enabled"] == 1


def test_a_sentence_kept_in_one_session_is_not_kept_again_in_another(store):
    from commons_memory.capture import Capture
    c = Capture(store)
    a = store.ensure_agent("repeater")
    line = "We decided to keep the notetaker worker on the server and move only the browser."
    for sid in ("r1", "r2", "r3"):
        c.event(a, "prompt", {"prompt": "please continue with the plan we agreed", "session_id": sid}, harness="claude-code")
        c.flush(a, None, sid, "claude-code", transcript=line, final=True)
    kept = store.q("SELECT COUNT(*) n FROM items WHERE kind IN ('observation','decision') AND content LIKE ?", ("%keep the notetaker worker%",))[0]["n"]
    assert kept == 1


def test_a_failed_index_rebuild_leaves_no_transaction_open(store):
    import pytest
    from commons_memory.librarian import Librarian
    lib = Librarian(store)
    store.add_item("memory", "the index rebuild must not leave a transaction open")
    store.x("DROP TABLE librarian_index")
    with pytest.raises(Exception):
        lib.build_index()
    assert not store.db.in_transaction  # open, every later write joined it and none was committed
    store.add_item("memory", "written after the failure")
    import sqlite3
    other = sqlite3.connect(store.path)  # a second connection sees only what was committed
    assert other.execute("SELECT COUNT(*) FROM items WHERE content='written after the failure'").fetchone()[0] == 1


def test_redacting_a_token_after_an_escape_keeps_json_valid():
    import json
    from commons_memory.redact import redact
    body = json.dumps({"message": "fix\nAbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCd tail", "ahead_by": 0})
    out, found = redact(body)
    assert "high_entropy" in found and "AbCdEfGh" not in out and json.loads(out)["ahead_by"] == 0
