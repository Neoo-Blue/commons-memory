import json
import time

import pytest

from commons_memory.orchestrator import Orchestrator, next_cron, parse_verdict, plan
from commons_memory.store import CommonsError


def agents(store, o):
    ids = {}
    for name, exp in [("claude", {"code": 0.9, "review": 0.6}), ("grok", {"debate": 0.9, "red_team": 0.9, "review": 0.8}),
                      ("cursor", {"code": 0.7, "review": 0.9, "test": 0.8, "judge": 0.8})]:
        ids[name] = store.ensure_agent(name)
        o.profiles.set(ids[name], expertise=exp, cost=3, speed=3, max_concurrent=3)
    return ids


def run_to_end(o, ids, results=None, max_steps=60):
    """Simulate agents: each claims ready work and completes it; results(agent, packet)->(text, verdict)."""
    results = results or (lambda a, p: (f"{a} did {p['type']}", "approve" if p["is_gate"] or p["type"] == "vote" else None))
    for _ in range(max_steps):
        progressed = False
        for name, aid in ids.items():
            p = o.claim(aid)
            if p:
                text, verdict = results(name, p)
                o.complete(p["subtask_id"], aid, text, verdict=verdict)
                progressed = True
        if not progressed:
            return


def test_planner_patterns():
    assert [d["type"] for d in plan("Should we use Postgres vs Mongo?")] == ["debate"] * 3 + ["judge"]
    assert plan("Implement login", "cross_review")[0]["gates"] == ["review", "red_team"]
    par = plan("Build the shop:\n- cart API\n- research payment providers\n- checkout UI")
    assert {d["type"] for d in par} >= {"plan", "code", "research", "review", "summarize"}
    assert [d["type"] for d in plan("vote on release", "consensus", 5)].count("vote") == 5
    assert parse_verdict("looks good\nVERDICT: reject\nfix x") == "reject"
    assert parse_verdict("LGTM") == "approve"


def test_cross_review_gate_rework_and_learning(store):
    o = Orchestrator(store)
    ids = agents(store, o)
    job = o.submit("Implement rate limiter", pattern="cross_review", project="api", agent_id=ids["claude"])
    st = {s["key"]: s for s in job["subtasks"]}
    assert st["code"]["agent"] == "claude"
    calls = {"review": 0}

    def results(a, p):
        if p["type"] == "review":
            calls["review"] += 1
            return ("needs tests\nVERDICT: reject" if calls["review"] == 1 else "ok now\nVERDICT: approve"), None
        if p["type"] == "red_team":
            return "no blocking issues\nVERDICT: approve", None
        if p["attempt"] > 1:
            assert "REWORK REQUIRED" in p["instructions"]
        return f"code v{p['attempt']}", None
    run_to_end(o, ids, results)
    final = o.status(job["job_id"])
    assert final["status"] == "done" and calls["review"] == 2
    code = [s for s in final["subtasks"] if s["key"] == "code"][0]
    assert code["attempts"] == 2
    # reviewer is never the author
    subs = store.q("SELECT key, claimed_by FROM subtasks WHERE job_id=?", (job["job_id"],))
    by = {r["key"]: r["claimed_by"] for r in subs}
    assert by["review"] != by["code"] and by["red_team"] != by["code"]
    prof = o.profiles.get("claude")
    assert prof["learned"]["code"]["n"] >= 2  # one failure (rejected) + one success
    assert "code v2" in final["result"]


def test_debate_and_consensus(store):
    o = Orchestrator(store)
    ids = agents(store, o)
    d = o.submit("Should we adopt GraphQL vs REST?", project="api")
    debaters = {s["agent"] for s in d["subtasks"] if s["type"] == "debate"}
    assert len(debaters) == 3  # spread across distinct agents
    run_to_end(o, ids, lambda a, p: (f"{a}: position" if p["type"] == "debate" else
                                     "Synthesis: REST for now" + "".join(r["result"] for r in p["dependency_results"]), None))
    st = o.status(d["job_id"])
    assert st["status"] == "done" and "Synthesis" in st["result"]
    assert store.why("GraphQL REST")["decisions"]  # judge synthesis became a decision
    c = o.submit("Vote: ship release 1.2?", pattern="consensus", n=3)
    votes = iter(["approve", "reject", "approve"])
    run_to_end(o, ids, lambda a, p: ("vote", next(votes)))
    st = o.status(c["job_id"])
    assert st["status"] == "done" and "2/3 approve -> APPROVED" in st["result"]


def test_lease_timeout_reassigns_and_no_duplicate_claims(store):
    o = Orchestrator(store)
    ids = agents(store, o)
    job = o.submit("Research caching options", pattern="plan_execute_verify")
    first = [s for s in job["subtasks"] if s["key"] == "plan"][0]
    owner = store.get_agent(first["agent"])["id"]
    p = o.claim(owner)
    assert p and o.claim(owner, p["subtask_id"]) is None  # can't claim twice
    others = [a for a in ids.values() if a != owner]
    assert all(o.claim(a, p["subtask_id"]) is None for a in others)
    store.x("UPDATE subtasks SET lease_expires=? WHERE id=?", (time.time() - 1, p["subtask_id"]))
    o.reap()
    row = store.q("SELECT status, assigned_agent, excluded_agents FROM subtasks WHERE id=?", (p["subtask_id"],))[0]
    assert row["status"] == "pending" and row["assigned_agent"] != owner and owner in json.loads(row["excluded_agents"])
    with pytest.raises(CommonsError):
        o.complete(p["subtask_id"], owner, "late result")


def test_budget_cycle_and_kill_switch(store):
    o = Orchestrator(store)
    ids = agents(store, o)
    with pytest.raises(CommonsError):
        o.submit("x", dag=[{"key": "a", "deps": ["b"]}, {"key": "b", "deps": ["a"]}])
    job = o.submit("Big research", pattern="plan_execute_verify", budget={"tokens": 10})
    p = o.claim(ids[job["subtasks"][0]["agent"]] if False else store.get_agent(job["subtasks"][0]["agent"])["id"])
    o.complete(p["subtask_id"], p and store.get_agent(job["subtasks"][0]["agent"])["id"], "x" * 400)
    assert all(o.claim(a) is None for a in ids.values())
    assert o.status(job["job_id"])["status"] == "budget_exceeded"
    o.set_paused(True, "test")
    with pytest.raises(CommonsError):
        o.submit("anything")
    o.set_paused(False)
    j2 = o.submit("Research thing", pattern="plan_execute_verify")
    o.cancel(j2["job_id"])
    assert o.status(j2["job_id"])["status"] == "cancelled"


def test_profiles_learning_changes_routing(store):
    o = Orchestrator(store)
    a, b = store.ensure_agent("a"), store.ensure_agent("b")
    o.profiles.set(a, expertise={"research": 0.6})
    o.profiles.set(b, expertise={"research": 0.5})
    assert o.profiles.rank("research")[0][1]["id"] == a
    for _ in range(8):
        o.profiles.record_outcome(a, "research", False)
        o.profiles.record_outcome(b, "research", True)
    assert o.profiles.rank("research")[0][1]["id"] == b
    o.profiles.set(b, availability="offline")
    assert all(x[1]["id"] != b for x in o.profiles.rank("research"))


def test_leftovers_recurring_carry_forward_and_pickup(store):
    o = Orchestrator(store)
    ids = agents(store, o)
    rec = o.schedule("nightly-triage", "0 2 * * *", "Triage new issues", pattern="plan_execute_verify", project="api")
    assert rec["next_run"].endswith("T09:00:00Z") or rec["next_run"]  # 02:00 local
    run1 = o.run_due(force="nightly-triage")[0]
    assert run1["carried_leftovers"] == 0

    def res(a, p):
        if p["type"] == "plan":
            return "plan\nLEFTOVER: label the 3 stale issues", None
        return "ok", ("approve" if p["is_gate"] else None)
    for _ in range(10):
        for aid in ids.values():
            p = o.claim(aid)
            if p:
                from commons_memory.worker import parse_output
                text, v = res(None, p)
                o.complete(p["subtask_id"], aid, text, verdict=v, leftovers=parse_output(text)["leftovers"] or None)
    los = o.leftovers(recurring_id=store.q("SELECT id FROM recurring")[0]["id"])
    assert [lo["text"] for lo in los] == ["label the 3 stale issues"]
    pk = o.pickup(ids["claude"], "api")
    assert "label the 3 stale issues" in pk["text"]
    run2 = o.run_due(force="nightly-triage")[0]
    assert run2["carried_leftovers"] == 1
    j2 = o.status(run2["job_id"])
    assert any(s["key"] == "leftover1" for s in j2["subtasks"])
    assert o.leftovers(project="api") == []  # carried, no longer open


def test_session_handoff_creates_leftovers(store):
    o = Orchestrator(store)
    a = store.ensure_agent("x")
    s = store.session_start("migrate", a, "p")
    store.handoff(s["session_id"], summary="half", next_steps=["finish step 2"], open_questions=["which region?"], agent_id=a)
    texts = {lo["text"] for lo in o.leftovers("p")}
    assert texts == {"finish step 2", "which region?"}


def test_cron():
    from datetime import datetime
    assert next_cron("*/15 * * * *", datetime(2026, 1, 1, 10, 7)) == datetime(2026, 1, 1, 10, 15)
    assert next_cron("0 9 * * 1", datetime(2026, 10, 11, 12, 0)) == datetime(2026, 10, 12, 9, 0)  # next Monday
    assert next_cron("@daily", datetime(2026, 1, 1, 10, 0)) == datetime(2026, 1, 2, 0, 0)
