import json
import os

import pytest

from commons_memory.retro import Checkpoints, RetroLearner, parse_chatgpt, parse_claude_ai, parse_claude_code, \
    parse_codex, parse_cursor, parse_gemini, parse_generic, parse_git, parse_memory_files
from retro_fixtures import SECRET, build


@pytest.fixture
def hist(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    p = build(home)
    monkeypatch.setenv("COMMONS_RETRO_HOME", str(home))
    monkeypatch.setenv("PATH", f"{p['bin']}{os.pathsep}{os.environ['PATH']}")
    for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, p["git_env"][k])
    return p


def _all(fn, store, paths=()):
    return list(fn(list(map(str, paths)), Checkpoints(store, dry=True)))


def _texts(convs):
    return " ".join(m["text"] for c in convs for m in c["messages"])


def test_parsers_each_format(store, hist):
    cc = _all(parse_claude_code, store)
    assert len(cc) == 1 and cc[0]["project"] == "shop" and cc[0]["model"] == "anthropic/claude-sonnet-4-5"
    assert "Stripe webhooks" in _texts(cc) and "export KEY" not in _texts(cc)  # tool calls dropped
    cx = _all(parse_codex, store)
    assert cx[0]["model"] == "openai/gpt-5-codex" and cx[0]["project"] == "api" and "environment_context" not in _texts(cx)
    cu = _all(parse_cursor, store)
    t = _texts(cu)
    assert "inline styles" in t and "CSS modules" in t and "pnpm" in t and "Tag the commit" in t
    gp = _all(parse_chatgpt, store)
    assert gp[0]["model"] == "openai/gpt-4o" and "ABANDONED" not in _texts(gp) and "code examples" in _texts(gp)
    ca = _all(parse_claude_ai, store)
    assert "Fly.io" in _texts(ca) and "Good choice" in _texts(ca)
    ge = _all(parse_gemini, store)
    assert {"Avoid adding new dependencies without asking.", "always write docstrings"} <= {m["text"] for c in ge for m in c["messages"]}
    assert "Cloud Run" in _texts(ge)
    gn = _all(parse_generic, store, [hist["generic"]])
    assert [m["role"] for m in gn[0]["messages"]] == ["user", "assistant"]
    mf = _all(parse_memory_files, store, [hist["memdir"], hist["mem0"]])
    stmts = [s[0] for c in mf for s in c["statements"]]
    assert {"Use pytest for all tests", "Prefer type hints everywhere", "Never commit .env files",
            "User's favourite editor is Neovim"} <= set(stmts)
    gt = _all(parse_git, store, [hist["repo"]])
    ids = {c["id"].split(":")[0] for c in gt}
    assert ids == {"commit", "pr"} and any("argon2" in _texts([c]) for c in gt)


def _run(store, hist, **kw):
    paths = {"generic": [str(hist["generic"])], "memory-files": [str(hist["memdir"]), str(hist["mem0"])],
             "git": [str(hist["repo"])]}
    return RetroLearner(store).run(["all"], paths, use_llm=False, **kw)


def test_dry_run_writes_nothing_and_estimates(store, hist):
    r = _run(store, hist, dry_run=True)
    assert r["dry_run"] and r["est_tokens"] > 0 and r["conversations"] >= 10 and r["preview"]
    assert not store.q("SELECT 1 FROM retro_queue") and not store.q("SELECT 1 FROM retro_checkpoints")
    assert r["est_cost"] == 0 and "heuristics" in r["note"]


def test_run_queue_review_apply_idempotent(store, hist):
    r = _run(store, hist)
    assert r["queued"] > 0 and not r["errors"], r["errors"]
    rl = RetroLearner(store)
    q = rl.queue()
    kinds = {x["kind"] for x in q}
    assert {"preference", "decision", "fact", "leftover", "performance", "skill"} <= kinds
    blob = json.dumps(q)
    assert SECRET not in blob  # redacted before extraction
    for x in q:
        assert x["source"]["kind"] and x["source"]["path"] and 0 < x["confidence"] <= 1
    dec = next(x for x in q if x["kind"] == "decision" and "argon2" in x["text"].lower())
    assert "72 bytes" in dec["payload"]["rationale"]
    assert any("retry queue" in x["text"] for x in q if x["kind"] == "leftover")
    assert not store.q("SELECT 1 FROM preferences WHERE source LIKE 'retro%'")  # nothing applied before review
    # second run: no new history -> nothing new
    r2 = _run(store, hist)
    assert r2["queued"] == 0 and r2["conversations"] == 0
    # append to the claude session -> only the new turn is read
    with open(hist["claude_jsonl"], "a") as f:
        f.write(json.dumps({"type": "user", "sessionId": "s1", "cwd": "/home/me/shop", "timestamp": "2026-03-06T00:00:00Z",
                            "message": {"role": "user", "content": "I prefer tabs over spaces in Makefiles."}}) + "\n")
    r3 = _run(store, hist)
    assert r3["conversations"] == 1 and r3["queued"] == 1
    # bulk approve/reject
    pend = rl.queue()
    pref = [x["id"] for x in pend if x["kind"] == "preference"]
    perf = [x["id"] for x in pend if x["kind"] == "performance"]
    skill = [x["id"] for x in pend if x["kind"] == "skill"]
    left = [x["id"] for x in pend if x["kind"] == "leftover"]
    decs = [x["id"] for x in pend if x["kind"] in ("decision", "fact")]
    res = rl.decide(pref + perf + skill + left + decs, True)
    assert res["decided"] == len(pref + perf + skill + left + decs) and not res["errors"], res
    stmts = [r["statement"] for r in store.q("SELECT statement FROM preferences")]
    assert any("small focused commits" in s for s in stmts) and any("tabs over spaces" in s for s in stmts)
    assert store.q("SELECT n FROM model_scores WHERE model_id='anthropic/claude-sonnet-4-5'")
    assert store.q("SELECT 1 FROM leftovers WHERE text LIKE '%retry queue%' AND status='open'")
    assert store.q("SELECT 1 FROM skills WHERE name LIKE 'retro-%'")
    it = store.q("SELECT meta, tags FROM items WHERE content LIKE '%argon2%'")[0]
    assert json.loads(it["meta"])["retro"] and "retro" in it["tags"]
    left_over = len(rl.queue("pending"))
    assert rl.decide("all", False)["decided"] == left_over
    assert not rl.queue("pending")


def test_auto_approve_with_floor(store, hist):
    r = _run(store, hist, auto_approve=True, min_confidence=0.75)
    assert 0 < r["applied"] < r["queued"]
    assert all(x["confidence"] < 0.75 for x in RetroLearner(store).queue("pending"))


def test_llm_extraction_budget_and_fallback(store, hist):
    from commons_memory.providers import ProviderLayer
    pl = ProviderLayer(store)
    pl.add_provider("mock", "mock", config={"models": [{"model": "m"}]})
    pl.refresh_models()
    pl.set_role("retro", "mock/m")
    rl = RetroLearner(store)
    d = rl.run(["claude-ai"], dry_run=True)
    assert d["model"] == "mock/m" and d["est_tokens"] > 0
    r = rl.run(["claude-ai"])
    assert r["llm_chunks"] == 1
    q = rl.queue()
    dec = next(x for x in q if x["kind"] == "decision")
    assert dec["method"] == "llm" and "regions" in dec["payload"]["rationale"]
    r0 = RetroLearner(store).run(["chatgpt"], budget=0)
    assert r0["budget_exhausted"] and r0["heuristic_chunks"] >= 1 and r0["llm_chunks"] == 0


def test_mcp_and_dashboard_endpoints(store, hist):
    from commons_memory.dashboard import API as Api
    api = Api(store)
    r = api.post("/api/retro/run", {"sources": ["gemini"], "dry_run": False, "use_llm": False})
    assert r["queued"] >= 2
    d = api.retro({})
    assert d["pending"] == r["queued"] and d["detected"]["gemini"]
    ids = [x["id"] for x in d["queue"]][:1]
    assert api.post("/api/retro/decide", {"ids": ids, "approve": False})["decided"] == 1
    assert api.retro({"status": "rejected"})["queue"][0]["id"] == ids[0]
