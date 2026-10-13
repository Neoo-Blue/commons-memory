"""v0.21: big work is split across agents (a model plans, else a live agent, else rules), agents split their own
subtasks, big hand-offs are planned, and routing weighs usage left and learned speed."""
import json

from commons_memory import planning as P
from commons_memory import quota as Q
from commons_memory import settings
from commons_memory.ids import now
from commons_memory.orchestrator import Orchestrator
from commons_memory.providers import ProviderLayer

BIG = "Ship the onboarding docs:\n- write the quickstart\n- write the troubleshooting page\n- record a demo"


def _agent(store, name, live=False, **exp):
    aid = store.ensure_agent(name)
    store.x("UPDATE agents SET profile=? WHERE id=?", (json.dumps({"expertise": exp or {"general": 0.6}}), aid))
    if live:  # a `commons agent daemon` heartbeat: woken within a second
        store.x("INSERT OR REPLACE INTO agent_presence(agent_id, client, last_heartbeat, updated_at) "
                "VALUES(?, 'daemon', ?, ?)", (aid, now(), now()))
    return aid


def _mock_model(store):
    ProviderLayer(store).add_provider("mock", "mock", config={"models": [{"model": "planner", "quality": 0.9}]})
    settings.put(store, "plan_chain", ["mock/planner", "agent", "rules"])


def _done(o, store, sid, result="done"):
    who = store.q("SELECT assigned_agent FROM subtasks WHERE id=?", (sid,))[0]["assigned_agent"]
    assert o.claim(who, sid), sid
    return o.complete(sid, who, result)


def test_a_model_plans_and_sees_the_team(store):
    _mock_model(store)
    a = _agent(store, "antigravity", live=True, research=0.9)
    Q.report(store, a, [{"window": "five_hour", "used_pct": 40, "resets_at": now() + 7200}])
    j = Orchestrator(store).submit("Build a CSV importer with docs", agent_id=a)
    assert j["pattern"] == "llm" and len(j["subtasks"]) == 3
    p = P.prompt(store, "x")
    assert '"agent": "antigravity"' in p and '"state": "live"' in p and "usage_left" in p and "1 agent(s) are live" in p


def test_without_a_model_a_live_agent_plans_and_its_plan_becomes_the_parts(store):
    lead, gb = _agent(store, "lead"), _agent(store, "grok-build", live=True, plan=0.9)
    _agent(store, "antigravity", live=True, code=0.9)
    o = Orchestrator(store)
    j = o.submit("Port the importer to Go and document it", agent_id=lead)
    assert j["pattern"] == "agent-plan" and len(j["subtasks"]) == 1 and j["subtasks"][0]["agent"] == "grok-build"
    plan_id = j["subtasks"][0]["id"]
    parts = [{"key": "port", "type": "code", "title": "Port it", "body": "Port the importer to Go."},
             {"key": "docs", "type": "write", "title": "Document it", "body": "Write the docs.", "deps": ["port"]}]
    assert o.claim(gb, plan_id)
    o.complete(plan_id, gb, "Here is the plan:\n" + json.dumps(parts))
    st = o.status(j["job_id"])
    assert st["status"] == "running" and len(st["subtasks"]) == 3
    port = next(x for x in st["subtasks"] if x["key"] == "p.port")
    assert json.loads(store.q("SELECT deps FROM subtasks WHERE id=?", (port["id"],))[0]["deps"]) == [plan_id]
    _done(o, store, port["id"], "ported")
    _done(o, store, next(x["id"] for x in st["subtasks"] if x["key"] == "p.docs"), "documented")
    job = store.q("SELECT status, result FROM jobs WHERE id=?", (j["job_id"],))[0]
    assert job["status"] == "done" and "documented" in job["result"] and '"key"' not in job["result"]


def test_an_unusable_agent_plan_or_nobody_live_falls_back_to_the_rules(store):
    lead, gb = _agent(store, "lead"), _agent(store, "grok-build", live=True, plan=0.9)
    o = Orchestrator(store)
    j = o.submit("Fix the flaky upload test", agent_id=lead)
    plan_id = j["subtasks"][0]["id"]
    o.claim(gb, plan_id)
    o.complete(plan_id, gb, "I would start by looking at the logs.")  # no JSON plan
    assert len(o.status(j["job_id"])["subtasks"]) > 1
    store.x("DELETE FROM agent_presence")
    assert o.submit("Fix the flaky download test", agent_id=lead)["pattern"] == "auto"  # nobody live: rules


def test_an_agent_splits_its_subtask_and_merges_the_parts(store):
    a, b, c = _agent(store, "a"), _agent(store, "b"), _agent(store, "c")
    o = Orchestrator(store)
    j = o.submit("Big audit", agent_id=a, dag=[{"key": "audit", "type": "review", "title": "Audit everything",
                                                  "body": "Audit the repo.", "assign": "a"}])
    sid = j["subtasks"][0]["id"]
    assert o.claim(a, sid)
    out = o.split(sid, a, ["Audit the API layer", {"title": "Audit the UI", "body": "Check the dashboard."}])
    assert len(out["parts"]) == 2 and all(p["assigned_to"] for p in out["parts"])
    assert o.claim(a, sid) is None  # waits for its parts
    for p in out["parts"]:
        _done(o, store, p["subtask_id"], f"findings for {p['title']}")
    assert store.q("SELECT 1 FROM messages WHERE recipient=? AND body LIKE ?", (a, f"%{sid}%"))  # woken to merge
    pk = o.claim(a, sid)
    assert len(pk["dependency_results"]) == 2 and "MERGE" in pk["instructions"]
    o.complete(sid, a, "Merged audit. VERDICT: approve")
    assert store.q("SELECT status FROM jobs WHERE id=?", (j["job_id"],))[0]["status"] == "done"


def test_a_big_hand_off_is_planned_across_the_team_favouring_the_recipient(store):
    _mock_model(store)
    sender, muse = _agent(store, "sender"), _agent(store, "muse", live=True)
    _agent(store, "antigravity", live=True)
    d = Orchestrator(store).delegate(sender, "muse", BIG)
    assert d["split"] and len(d["parts"]) == 3 and "muse" in [p["assigned_to"] for p in d["parts"]]
    assert store.q("SELECT value FROM meta WHERE key=?", (f"prefer:{d['job_id']}",))[0]["value"] == muse
    assert "split" not in Orchestrator(store).delegate(sender, "muse", "check one thing")  # small: stays whole


def test_usage_left_and_learned_speed_steer_routing(store):
    busy, fresh = _agent(store, "busy", code=0.7), _agent(store, "fresh", code=0.7)
    Q.report(store, busy, [{"window": "five_hour", "used_pct": 50, "resets_at": now() + 7200}])
    Q.report(store, fresh, [{"window": "five_hour", "used_pct": 5, "resets_at": now() + 7200}])
    o = Orchestrator(store)
    j = o.submit("one fix", dag=[{"key": "k", "type": "code", "title": "fix it"}])
    assert j["subtasks"][0]["agent"] == "fresh"
    for aid, secs in ((busy, 30), (fresh, 300)):
        for _ in range(3):
            o.profiles.record_outcome(aid, "write", True, seconds=secs)
    assert o.profiles.score(store.get_agent("busy"), "write") > o.profiles.score(store.get_agent("fresh"), "write")


def test_a_split_that_loses_its_claim_midway_leaves_nothing_behind(store, monkeypatch):
    import pytest
    from commons_memory.store import CommonsError
    a = _agent(store, "a")
    o = Orchestrator(store)
    sid = o.submit("x", dag=[{"key": "k", "title": "t", "assign": "a"}])["subtasks"][0]["id"]
    o.claim(a, sid)
    real = o._insert

    def insert_then_lose_claim(*args, **kw):  # the lease is reaped while the parts are being written
        ids = real(*args, **kw)
        store.db.execute("UPDATE subtasks SET claimed_by='someone-else' WHERE id=?", (sid,))
        return ids
    monkeypatch.setattr(o, "_insert", insert_then_lose_claim)
    with pytest.raises(CommonsError, match="before the split"):
        o.split(sid, a, ["one", "two"])
    assert store.q("SELECT COUNT(*) n FROM subtasks")[0]["n"] == 1
    assert store.q("SELECT claimed_by FROM subtasks WHERE id=?", (sid,))[0]["claimed_by"] == a  # rolled back


def test_a_check_step_always_waits_for_the_work_it_checks(store):
    a, b = _agent(store, "a", code=0.9), _agent(store, "b", review=0.9)
    o = Orchestrator(store)
    j = o.submit("x", dag=[{"key": "code", "type": "code", "title": "build", "gates": ["rev"]},
                           {"key": "rev", "type": "review", "title": "review it"}])  # the review names no deps
    ids = {x["key"]: x["id"] for x in j["subtasks"]}
    assert json.loads(store.q("SELECT deps FROM subtasks WHERE id=?", (ids["rev"],))[0]["deps"]) == [ids["code"]]
    assert o.claim(b, ids["rev"]) is None and o.claim(a, ids["rev"]) is None


def test_a_failed_planning_step_falls_back_to_the_rules_at_once(store):
    lead, gb = _agent(store, "lead"), _agent(store, "grok-build", live=True, plan=0.9)
    o = Orchestrator(store)
    j = o.submit("Fix the flaky upload test", agent_id=lead)
    plan_id = j["subtasks"][0]["id"]
    o.claim(gb, plan_id)
    o.complete(plan_id, gb, "ran out of context", success=False)
    st = o.status(j["job_id"])
    assert st["status"] == "running" and len(st["subtasks"]) > 1
    assert store.q("SELECT status FROM subtasks WHERE id=?", (plan_id,))[0]["status"] == "done"


def test_a_window_that_already_reset_is_not_expiring(store):
    a = _agent(store, "a")
    Q.report(store, a, [{"window": "five_hour", "used_pct": 10, "resets_at": now() - 3600}])
    assert Q.headroom(store, a) == (0.8 - Q.task_cost(store, a, "general", "five_hour") / 100, False)
