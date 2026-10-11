import time

from commons_memory.orchestrator import Orchestrator
from commons_memory.prefs import Preferences, effective_confidence
from commons_memory.usage import Usage, estimate_cost


def test_observe_reinforce_scope_decay(store):
    p = Preferences(store)
    a = p.observe("Use pnpm, not npm", project="web", confidence=0.6)
    b = p.observe("use pnpm not npm", project="web", confidence=0.6)
    assert b["reinforced"] and b["id"] == a["id"] and b["confidence"] > 0.6
    p.observe("Write commit messages in imperative mood")
    p.observe("Prefer pytest fixtures over setUp", task_type="test")
    web = p.get(project="web")["text"]
    assert "pnpm" in web and "imperative" in web and "pytest" not in web
    assert "pytest" in p.get(task_type="test")["text"]
    assert "pnpm" not in p.get(project="other")["text"]
    assert effective_confidence(1.0, time.time() - 120 * 86400) < 0.51
    store.x("UPDATE preferences SET last_confirmed=? WHERE id=?", (time.time() - 1000 * 86400, a["id"]))
    assert "pnpm" not in p.get(project="web")["text"]  # decayed away
    p.confirm(a["id"])
    assert "pnpm" in p.get(project="web")["text"]


def test_extract_from_corrections_and_privacy(store):
    p = Preferences(store)
    got = p.extract("No, use tabs instead. I prefer short functions. Never add emojis to commit messages.", project="x")
    stmts = " | ".join(g["statement"] for g in got)
    assert "tabs" in stmts and "short functions" in stmts and "emojis" in stmts
    assert len(p.get(project="x", max_tokens=1000)["preferences"]) == 3
    p.forget(got[0]["id"])
    assert len(p.get(project="x", max_tokens=1000)["preferences"]) == 2
    p.forget(everything=True)
    assert p.get(include_all=True)["preferences"] == []


def test_preferences_injected_into_pickup_and_packets(store):
    o = Orchestrator(store)
    a = store.ensure_agent("cursor")
    o.profiles.set(a, expertise={"code": 0.9, "plan": 0.9, "test": 0.9})
    o.prefs.observe("Always use type hints", project="api")
    assert "type hints" in o.pickup(a, "api")["text"]
    o.submit("Do the thing", pattern="plan_execute_verify", project="api")
    pk = o.claim(a)
    assert "type hints" in pk["preferences"]


def test_usage_cost_and_daily_cap(store, monkeypatch):
    assert estimate_cost("claude-sonnet-4", 1_000_000, 0) == 3.0
    assert estimate_cost("unknown-model", 1_000_000, 1_000_000) == 5.0
    u = Usage(store)
    a = store.ensure_agent("grok")
    r = u.report(a, tokens_in=1000, tokens_out=500, model="grok-4", project="p")
    assert r["cost"] > 0
    assert u.summary("agent")[0]["label"] == "grok" and u.summary("project")[0]["label"] == "p"
    monkeypatch.setenv("COMMONS_DAILY_COST_CAP", "0.01")
    r = u.report(a, tokens_in=10_000_000, model="grok-4")
    assert "paused" in r and Orchestrator(store).paused()
