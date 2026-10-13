import json
import time

from commons_memory import quota as Q
from commons_memory import schedules as SC
from commons_memory.orchestrator import Orchestrator


def _agents(s, o, *names, exp=None):
    ids = []
    for n in names:
        a = s.ensure_agent(n)
        o.profiles.set(a, expertise=(exp or {}).get(n, {"general": 0.8}), max_concurrent=4)
        ids.append(a)
    return ids


def _job(o, text="Write the weekly report"):
    return o.submit(text, dag=[{"key": "only", "type": "general", "title": text, "body": text, "deps": []}],
                    pattern="parallel")["job_id"]


def test_headroom_respects_the_users_reserve(store):
    a = store.ensure_agent("worker")
    soon = time.time() + 3600
    Q.report(store, a, [{"window": "five_hour", "used_pct": 70, "resets_at": soon}])
    assert Q.can_take(store, a)[0]          # 100 - 70 - 20 reserve - 5 task = 5 >= 0
    Q.report(store, a, [{"window": "five_hour", "used_pct": 80, "resets_at": soon}])
    ok, until, win = Q.can_take(store, a)
    assert not ok and abs(until - soon) < 1 and win == "five_hour"
    Q.report(store, a, [{"window": "five_hour", "used_pct": 99, "resets_at": time.time() - 5}])
    assert Q.can_take(store, a)[0]          # the window already reset: fresh until the next report
    st = Q.status(store)[0]
    assert st["agent"] == "worker" and st["windows"][0]["reserve_pct"] == 20.0


def test_allocation_skips_agents_at_their_limit_and_waits_when_all_are(store):
    o = Orchestrator(store)
    best, other = _agents(store, o, "best", "other", exp={"best": {"general": 0.95}, "other": {"general": 0.5}})
    reset = time.time() + 7200
    Q.report(store, best, [{"window": "weekly", "used_pct": 90, "resets_at": reset}])
    jid = _job(o)
    assert store.q("SELECT assigned_agent FROM subtasks WHERE job_id=?", (jid,))[0]["assigned_agent"] == other
    Q.report(store, other, [{"window": "five_hour", "used_pct": 95, "resets_at": reset - 3600}])
    jid2 = _job(o, "Draft the release notes")
    st = dict(store.q("SELECT * FROM subtasks WHERE job_id=?", (jid2,))[0])
    assert st["assigned_agent"] is None and abs(st["not_before"] - (reset - 3600)) < 1  # first agent free again
    assert o.claimable(other) == [] or all(c["job_id"] != jid2 for c in o.claimable(other))
    store.x("UPDATE subtasks SET not_before=? WHERE id=?", (time.time() - 1, st["id"]))
    Q.report(store, other, [{"window": "five_hour", "used_pct": 10, "resets_at": reset + 18000}])
    assert st["id"] in o.quota_wake()
    assert store.q("SELECT assigned_agent FROM subtasks WHERE id=?", (st["id"],))[0]["assigned_agent"] == other


def test_pause_keeps_progress_and_resumes_after_the_reset(store):
    o = Orchestrator(store)
    (w,) = _agents(store, o, "solo")
    jid = _job(o)
    sid = store.q("SELECT id FROM subtasks WHERE job_id=?", (jid,))[0]["id"]
    assert o.claim(w, sid)
    r = o.pause(sid, w, "Sections 1-3 drafted in /drive/report.md; 4 and 5 left", resume_at=time.time() + 3600)
    assert r["paused_until"]
    assert o.claimable(w) == []                                   # parked until the reset
    store.x("UPDATE subtasks SET not_before=? WHERE id=?", (time.time() - 1, sid))
    o.quota_wake()
    packet = o.claim(w, sid)
    assert packet["resume_from_checkpoint"].startswith("Sections 1-3") and packet["attempt"] == 2


def test_exhausted_parks_everything_the_agent_holds(store):
    o = Orchestrator(store)
    (w,) = _agents(store, o, "tired")
    sid = store.q("SELECT id FROM subtasks WHERE job_id=?", (_job(o),))[0]["id"]
    o.claim(w, sid)
    res = o.exhausted(w, time.time() + 1800, "half done: tests written, implementation pending")
    assert res["paused"] == [sid] and not Q.can_take(store, w)[0]
    st = store.q("SELECT status, checkpoint, not_before FROM subtasks WHERE id=?", (sid,))[0]
    assert st["status"] == "pending" and "tests written" in st["checkpoint"] and st["not_before"] > time.time()


def test_reserve_learns_the_users_own_burn_rate(store):
    a = store.ensure_agent("claude-like")
    t0 = time.time() - 14 * 86400
    rows = []
    for d in range(14):  # the user works 9:00-12:00 UTC every day, burning 4 %/h; Commons idle meanwhile
        base = t0 + d * 86400 - (t0 % 86400) + 9 * 3600
        for h in range(4):
            rows.append(("claude-like-w", base + h * 3600, 10 + 4 * h))
    for _w, ts, used in rows:
        store.x("INSERT INTO quota_log(agent_id,win,used_pct,ts,busy) VALUES(?,?,?,?,0)", (a, "weekly", used, ts))
    need = Q.user_need(store, a, "weekly", time.time() + 7 * 86400)
    assert need > 20, need  # ~4 %/h over the user's usual hours in the next week beats the 20 % floor
    w = {"window": "weekly", "period": "weekly", "resets_at": time.time() + 7 * 86400}
    assert Q.reserve(store, a, w) == need


def test_completed_task_teaches_its_quota_cost(store):
    o = Orchestrator(store)
    (w,) = _agents(store, o, "measured")
    Q.report(store, w, [{"window": "five_hour", "used_pct": 10, "resets_at": time.time() + 9000}])
    sid = store.q("SELECT id FROM subtasks WHERE job_id=?", (_job(o),))[0]["id"]
    o.claim(w, sid)
    time.sleep(0.01)
    Q.report(store, w, [{"window": "five_hour", "used_pct": 17, "resets_at": time.time() + 9000}])
    o.complete(sid, w, "done")
    assert Q.task_cost(store, w, "general", "five_hour") == 7.0


def test_missed_or_blocked_scheduled_runs_are_taken_over_once(store):
    o = Orchestrator(store)
    owner, helper, checker = _agents(store, o, "leads-bot", "helper", "checker")
    SC.register(store, owner, "daily-leads", "0 9 * * *", "Scrape the SMB list and email the top 10",
                acceptance="- 10 leads\n- each with a source URL", how="runs scrape.py then send.py", tz="UTC")
    SC.ran(store, owner, "daily-leads", "ok", "10 leads sent", "out/2026-10-01.csv")
    day = 86400
    slot = (time.time() // day + 2) * day + 9 * 3600            # a 09:00 UTC run two days ahead
    assert SC.watch(store, slot + 600) == []                     # within grace, owner fine: wait
    took = SC.watch(store, slot + 1801)                          # grace passed, no ok report: take over
    assert len(took) == 1 and "no ok report" in took[0]["reason"]
    jid = took[0]["job_id"]
    subs = store.q("SELECT excluded_agents, assigned_agent, body FROM subtasks WHERE job_id=?", (jid,))
    assert all(owner in json.loads(x["excluded_agents"]) and x["assigned_agent"] != owner for x in subs)
    job = store.q("SELECT body FROM jobs WHERE id=?", (jid,))[0]["body"]
    assert "each with a source URL" in job and "10 leads sent" in job   # acceptance bar + quality reference travel
    assert SC.watch(store, slot + 1900) == []                     # never twice for the same run
    store.x("UPDATE jobs SET status='done' WHERE id=?", (jid,))
    SC.watch(store, slot + 2000)
    assert store.q("SELECT last_status FROM recurring WHERE name='daily-leads'")[0]["last_status"].startswith("covered")
    # next day the owner is out of quota until well after the grace window: take over at the slot, don't wait
    Q.report(store, owner, [{"window": "weekly", "used_pct": 100, "resets_at": slot + 3 * day}])
    early = SC.watch(store, slot + day + 60)
    assert len(early) == 1 and "out of quota" in early[0]["reason"]
    assert o.run_due(at=slot + day + 60) == [] or all(r["recurring"] != "daily-leads" for r in o.run_due(at=slot + day + 61))


def test_owner_reporting_a_good_run_means_no_takeover(store):
    o = Orchestrator(store)
    owner, _h = _agents(store, o, "own", "h2")
    SC.register(store, owner, "nightly", "0 2 * * *", "Back up the database", tz="UTC")
    store.x("UPDATE recurring SET last_ok=? WHERE name='nightly'", (time.time() + 10 * 86400,))
    assert SC.watch(store, time.time() + 3 * 86400) == []
    assert SC.listing(store, "own")[0]["delivered"] is True


def test_question_waits_for_the_user_and_the_answer_reaches_the_agent(store):
    from commons_memory import board as B
    from commons_memory.messaging import Messaging
    a = store.ensure_agent("asker")
    ask = B.add(store, "ask", "Ship the pricing page", project="site")
    q = B.add(store, "question", "Monthly or annual price first?", project="site", parent=ask["id"], agent_id=a)
    B.add(store, "action", "Build the page", project="site", parent=ask["id"])
    assert q["status"] == "blocked"
    needs = B.needs_user(store, "site")
    assert [n["id"] for n in needs] == [q["id"]] and needs[0]["holding_up"] == 1
    assert "NEEDS THE USER" in B.view(store, "site")["text"]
    B.answer(store, q["id"], "Annual first")
    assert B.get(store, q["id"])["status"] == "done" and B.needs_user(store, "site") == []
    assert "Annual first" in json.dumps(Messaging(store).recv(a))


def test_keep_working_blocks_stop_only_while_work_waits_and_quota_allows(store, monkeypatch):
    from commons_memory.capture import Capture
    o = Orchestrator(store)
    (w,) = _agents(store, o, "worker2")
    cap = Capture(store)
    stop = {"session_id": "s1", "keep_working": True}
    assert "block" not in cap.event(w, "stop", stop, harness="claude-code")          # nothing waiting
    _job(o)
    assert "claim_subtask" in cap.event(w, "stop", stop, harness="claude-code")["block"]
    monkeypatch.setenv("COMMONS_HOOK_KEEP_WORKING_MAX", "2")
    cap.event(w, "stop", stop, harness="claude-code")
    assert "block" not in cap.event(w, "stop", stop, harness="claude-code")          # bounded: never loops forever
    Q.report(store, w, [{"window": "five_hour", "used_pct": 95, "resets_at": time.time() + 3600}])
    assert "block" not in cap.event(w, "stop", stop | {"session_id": "s2"}, harness="claude-code")  # user's reserve


def test_turn_hook_captures_chat_agents_and_recalls(store):
    from commons_memory.capture import Capture
    a = store.ensure_agent("chatbot")
    store.add_item("memory", "The staging database lives on the NAS at port 5433", agent_id=a)
    cap, pl = Capture(store), {"session_id": "c1"}
    ctx = cap.event(a, "prompt", pl | {"prompt": "where does the staging database live on the NAS"}, harness="mcp")["context"]
    assert "5433" in ctx
    r = cap.event(a, "stop", pl | {"transcript_tail": "assistant: We decided to move staging to port 5434 because 5433 clashes."},
                  harness="mcp")
    assert r["observations"] >= 1
    said = [x["content"] for x in store.q("SELECT content FROM items WHERE session_id='mcp:c1' ORDER BY created_at")]
    assert said == ["where does the staging database live on the NAS",
                    "We decided to move staging to port 5434 because 5433 clashes."]   # everything, word for word
    cap.event(a, "stop", pl | {"transcript_tail": "assistant: We decided to move staging to port 5434 because 5433 clashes."},
              harness="mcp")
    assert len(store.q("SELECT 1 FROM items WHERE session_id='mcp:c1'")) == 2          # a resent tail is not stored twice
    ctx2 = cap.event(a, "prompt", pl | {"prompt": "where does the staging database live again"}, harness="mcp")["context"]
    assert "where does the staging database live on the NAS" not in ctx2                # own turns aren't recalled
    other = cap.event(a, "prompt", {"session_id": "c2", "prompt": "why did we move staging to another port"}, harness="mcp")
    assert "5434" in other["context"]                                                   # another chat finds it


def test_replies_parses_a_transcript_tail():
    from commons_memory.capture import replies
    tail = "cut off end of an older reply\nuser: do the thing\nassistant: Done.\nIt had two lines.\nuser: thanks\nassistant: ok"
    assert replies(tail) == ["Done.\nIt had two lines.", "ok"]


def test_relayed_agent_reports_are_not_user_prompts(store):
    from commons_memory.capture import Capture
    a = store.ensure_agent("cc")
    Capture(store).event(a, "prompt", {"session_id": "n1", "prompt": "Another Claude session sent a message:\n"
                                       "<agent-message from=\"x\">report</agent-message>"}, harness="claude-code")
    assert not store.q("SELECT 1 FROM items WHERE session_id='claude-code:n1'")


def test_no_missed_run_takeover_for_an_owner_that_never_reports(store):
    o = Orchestrator(store)
    owner, _h = _agents(store, o, "quiet", "h3")
    SC.register(store, owner, "mailer", "0 9 * * *", "Send the digest email", tz="UTC")
    store.x("UPDATE recurring SET created_at=? WHERE name='mailer'", (time.time() - 5 * 86400,))
    assert SC.watch(store, time.time() + 86400) == []   # it may well have run; a takeover could send it twice


def test_review_fixes_hooks_forget_drive_keepwork(store, tmp_path, monkeypatch):
    import json as _j
    from commons_memory.capture import Capture, run_hook
    from commons_memory.drive import Drive
    a = store.ensure_agent("gem")
    # Gemini/Cursor hand over the raw last reply (no transcript): it is still kept verbatim
    monkeypatch.delenv("COMMONS_URL", raising=False)
    run_hook("gemini", "AfterAgent", _j.dumps({"session_id": "g1", "prompt_response": "Deployed build 42 to staging.",
                                               "cwd": str(tmp_path)}), store_factory=lambda: store)
    assert store.q("SELECT 1 FROM items WHERE content='Deployed build 42 to staging.'")
    # a Claude Code reply holding a "user: ..." line is stored whole
    tr = tmp_path / "t.jsonl"
    reply = "Set the owner:\nuser: appuser\ngroup: staff"
    tr.write_text("{}\n" + _j.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": reply}]}}) + "\n")
    run_hook("claude-code", "Stop", _j.dumps({"session_id": "c9", "transcript_path": str(tr), "cwd": str(tmp_path)}),
             store_factory=lambda: store)
    assert store.q("SELECT 1 FROM items WHERE content=?", (reply,))
    # agents can't forget what the user said
    from commons_memory.server import build_server
    Capture(store).event(a, "prompt", {"session_id": "f1", "prompt": "never delete my words please"}, harness="mcp")
    uid = store.q("SELECT id FROM items WHERE content='never delete my words please'")[0]["id"]
    srv = build_server(store)
    tool = srv._tool_manager.get_tool("forget").fn
    try:
        import asyncio
        asyncio.run(tool(id=uid, agent="gem"))
        raise AssertionError("forget deleted a conversation record")
    except Exception as e:  # noqa: BLE001
        assert "only be removed by the user" in str(e)
    # an empty text file in the Drive is fine
    Drive(store).write("proj/__init__.py", b"", agent_id=a)
    # keep-working stays spent after its max instead of restarting
    o = Orchestrator(store)
    (w,) = _agents(store, o, "kw")
    _job(o)
    cap, stop = Capture(store), {"session_id": "k1", "keep_working": 2}
    assert cap.event(w, "stop", stop, harness="claude-code").get("block")
    assert cap.event(w, "stop", stop, harness="claude-code").get("block")
    assert not cap.event(w, "stop", stop, harness="claude-code").get("block")
    assert not cap.event(w, "stop", stop, harness="claude-code").get("block")   # still spent, not a fresh 2 rounds
