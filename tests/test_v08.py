import json
import os
import time

import pytest
from conftest import make_store

from commons_memory import ranking as R
from commons_memory.capture import (
    Capture,
    heuristic_observations,
    install_hooks,
    output_for,
    run_hook,
    strip_private,
)
from commons_memory.embed import HashEmbedder, OpenAICompatEmbedder, get_embedder

DAY = 86400


def test_auto_embedder_falls_back_and_openai_compat(monkeypatch):
    monkeypatch.setenv("COMMONS_EMBEDDER", "auto")
    import commons_memory.embed as E
    monkeypatch.setattr(E, "fastembed_available", lambda: False)
    monkeypatch.delenv("COMMONS_EMBED_URL", raising=False)
    E._CACHE.clear()
    assert isinstance(get_embedder(), HashEmbedder)
    # OpenAI-compatible endpoint (mocked transport)
    import io
    calls = []

    def fake_urlopen(req, timeout=0):
        b = json.loads(req.data)
        calls.append((req.full_url, req.headers.get("Authorization"), b))
        data = [{"index": i, "embedding": [1.0, float(len(t)), 0.0]} for i, t in enumerate(b["input"])]
        return io.BytesIO(json.dumps({"data": data}).encode())
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("EMB_KEY", "sk-test")
    monkeypatch.setenv("COMMONS_EMBED_KEY_REF", "env://EMB_KEY")
    e = OpenAICompatEmbedder(url="http://127.0.0.1:11434/v1", model="nomic-embed-text")
    assert e.name == "openai:nomic-embed-text" and e.dim == 3
    v = e.embed(["ab", "abcd"])
    assert abs(sum(x * x for x in v[0]) - 1) < 1e-6
    assert calls[0][0] == "http://127.0.0.1:11434/v1/embeddings" and calls[0][1] == "Bearer sk-test"


def test_reindex_switches_vector_space(tmp_path):
    s = make_store(tmp_path)
    for i in range(5):
        s.add_item("memory", f"note number {i} about kubernetes upgrades")
    assert s.reindex()["done"] == 0  # already embedded with hash
    s.embedder = type("E", (HashEmbedder,), {"name": "hash2"})()
    r = s.reindex(batch=2)
    assert r == {"embedder": "hash2", "done": 5, "remaining": 0}
    assert s.q("SELECT COUNT(*) n FROM embeddings WHERE model='hash2'")[0]["n"] == 5
    s.defer_embed = True
    s.add_item("memory", "deferred one")
    assert s.reindex()["done"] == 1


def test_time_window_parsing():
    ref = time.mktime((2024, 6, 15, 12, 0, 0, 0, 0, 0))
    a, b = R.time_window("what did I do last week?", ref)
    assert a < ref - 7 * DAY < b
    a, b = R.time_window("where was I two months ago", ref)
    assert a < ref - 60 * DAY < b
    a, b = R.time_window("the trip in March", ref)
    assert time.gmtime(a).tm_mon == 3 and time.gmtime(a).tm_year == 2024
    a, b = R.time_window("in December", ref)
    assert time.gmtime(a).tm_year == 2023  # most recent past December
    assert R.time_window("on 2024-05-07 what happened", ref)[0] < time.mktime((2024, 5, 7, 12, 0, 0, 0, 0, 0))
    assert R.time_window("how do I configure nginx", ref) is None
    assert "what" not in R.terms("what is the capital")


def test_time_recency_and_entity_signals(tmp_path):
    s = make_store(tmp_path)
    ref = time.time()
    old = s.add_item("memory", "Dinner with Alice at the sushi place", created_at=ref - 60 * DAY)["id"]
    new = s.add_item("memory", "Dinner with Alice at the taco place", created_at=ref - 7 * DAY)["id"]
    r = s.search("dinner with Alice last week", explain=True, as_of=ref)
    assert r["results"][0]["id"] == new and r["explain"][new]["time"] == 1.0
    r = s.search("dinner with Alice two months ago", as_of=ref)
    assert r["results"][0]["id"] == old
    # entity boost: items linked to an entity named in the query rank first among equals
    a = s.add_item("memory", "quarterly planning notes for the platform", entities=["Orion"])["id"]
    b = s.add_item("memory", "quarterly planning notes for the platform team")["id"]
    ids, _, why = s.rank("Orion quarterly planning")
    assert ids.index(a) < ids.index(b) and why[a].get("entity") == 1


def test_capture_pipeline_and_privacy(tmp_path, monkeypatch):
    s = make_store(tmp_path)
    aid = s.ensure_agent("claude")
    c = Capture(s)
    s.add_item("decision", "We use Postgres for the main store", project="shop")
    ctx = c.event(aid, "session_start", {"cwd": str(tmp_path / "shop"), "session_id": "s1"}, "claude-code")["context"]
    assert "Commons memory (shop)" in ctx and "Postgres" in ctx
    c.event(aid, "prompt", {"project": "shop", "session_id": "s1",
                            "prompt": "fix the login bug <private>my password is hunter2</private>"}, "claude-code")
    c.event(aid, "tool", {"project": "shop", "session_id": "s1", "tool_name": "Edit",
                          "tool_input": {"file_path": "app/login.py"}, "tool_response": "ok"}, "claude-code")
    c.event(aid, "tool", {"project": "shop", "session_id": "s1", "tool_name": "Bash",
                          "tool_input": {"command": "pytest"}, "tool_response": "FAILED test_login"}, "claude-code")
    r = c.event(aid, "stop", {"project": "shop", "session_id": "s1", "transcript_tail":
                              "assistant: Fixed the bug: the root cause was a stale session cookie.\n"
                              "assistant: We decided to use signed cookies instead of server sessions.\n"
                              "assistant: TODO: still need to add a regression test for logout."}, "claude-code")
    kinds = {i["kind"] for i in r["items"]}
    assert {"bugfix", "decision", "todo", "change", "summary"} <= kinds
    assert not s.q("SELECT 1 FROM observations WHERE text LIKE '%hunter2%'")
    assert not s.q("SELECT 1 FROM items WHERE content LIKE '%hunter2%'")
    from commons_memory.orchestrator import Orchestrator
    assert any("regression test" in lo["text"] for lo in Orchestrator(s).leftovers("shop"))
    assert c.event(aid, "stop", {"project": "shop", "session_id": "s1"}, "claude-code") == {"ok": True, "observations": 0}
    # per-project opt-out
    c.configure("shop", enabled=False)
    assert c.event(aid, "prompt", {"project": "shop", "prompt": "x y z"}, "claude-code")["skipped"]
    assert len(c.stream("shop")) >= 5


def test_observer_llm_role_used_when_configured(tmp_path, monkeypatch):
    s = make_store(tmp_path)
    aid = s.ensure_agent("codex")
    from commons_memory.providers import ProviderLayer
    monkeypatch.setattr(ProviderLayer, "call_role", lambda self, role, prompt, max_tokens=0: json.dumps(
        {"summary": "Refactored auth.", "observations": [{"kind": "discovery", "text": "The API returns 409 on dup."}]})
        if role == "observer" else None)
    c = Capture(s)
    c.event(aid, "tool", {"project": "p", "session_id": "x", "tool_name": "Bash", "tool_input": "curl", "tool_response": "409"}, "codex")
    r = c.event(aid, "session_end", {"project": "p", "session_id": "x"}, "codex")
    assert r["via"] == "llm" and [i["kind"] for i in r["items"]] == ["discovery", "summary"]


def test_heuristics_and_private():
    assert strip_private("a <private>secret</private> b <PRIVATE>x") == "a [private] b [private]"
    obs = heuristic_observations([{"kind": "tool", "text": "Edit", "file": "a.py"}],
                                 "We found that the cache is never invalidated on deploy.")
    assert {o["kind"] for o in obs} == {"change", "discovery"}


def test_hook_client_local_and_offline_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COMMONS_AGENT", "claude")
    s = make_store(tmp_path)
    s.add_item("memory", "Project uses pnpm, not npm", project="web")
    payload = json.dumps({"session_id": "a", "cwd": str(tmp_path), "project": "web", "source": "startup"})
    out = run_hook("claude-code", "SessionStart", payload, store_factory=lambda: s)
    j = json.loads(out)
    assert j["hookSpecificOutput"]["hookEventName"] == "SessionStart" and "pnpm" in j["hookSpecificOutput"]["additionalContext"]
    assert run_hook("claude-code", "Notification", "{}") == ""  # unmapped events are ignored
    # transcript tail from a JSONL transcript (Claude Code shape)
    tr = tmp_path / "t.jsonl"
    tr.write_text("\n".join(json.dumps(o) for o in [
        {"type": "user", "message": {"role": "user", "content": "please fix it"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Fixed the bug in parser. <private>tok</private>"}]}}]))
    run_hook("claude-code", "Stop", json.dumps({"session_id": "a", "project": "web", "transcript_path": str(tr)}), store_factory=lambda: s)
    assert s.q("SELECT 1 FROM items WHERE kind='observation' AND content LIKE '%Fixed the bug in parser%'")
    assert not s.q("SELECT 1 FROM items WHERE content LIKE '%tok%' AND kind='observation'")
    # remote mode, server down -> queued, never raises, returns quickly
    monkeypatch.setenv("COMMONS_URL", "http://127.0.0.1:9")
    t = time.time()
    assert run_hook("codex", "PostToolUse", json.dumps({"tool_name": "Bash", "session_id": "b"})) == ""
    assert time.time() - t < 3
    q = tmp_path / "home" / "hook-queue.jsonl"
    assert json.loads(q.read_text().splitlines()[0])["event"] == "tool"


def test_cursor_and_gemini_outputs():
    assert json.loads(output_for("cursor", "sessionStart", "ctx")) == {"additional_context": "ctx"}
    assert output_for("cursor", "beforeSubmitPrompt", "ctx") == ""
    assert json.loads(output_for("gemini", "BeforeAgent", "c"))["hookSpecificOutput"]["additionalContext"] == "c"


@pytest.mark.parametrize("h", ["claude-code", "codex", "gemini", "cursor"])
def test_install_hooks_idempotent_and_preserves_config(tmp_path, h):
    p = tmp_path / "cfg.json"
    base = {"theme": "dark", "hooks": {}} if h != "cursor" else {"version": 1, "hooks": {"stop": [{"command": "./mine.sh"}]}}
    p.write_text(json.dumps(base))
    install_hooks(h, path=str(p), command="/opt/commons/bin/commons")
    install_hooks(h, path=str(p), command="/opt/commons/bin/commons")
    cfg = json.loads(p.read_text())
    blob = json.dumps(cfg)
    assert blob.count("commons hook ") == (7 if h == "cursor" else 6)
    if h == "cursor":
        assert {"command": "./mine.sh"} in cfg["hooks"]["stop"]
    else:
        assert cfg["theme"] == "dark"
    install_hooks(h, path=str(p), uninstall=True)
    assert "commons hook " not in p.read_text()
    assert os.path.exists(str(p) + ".bak")


def test_hook_http_endpoint(tmp_path):
    from starlette.testclient import TestClient

    from commons_memory.agent_http import build_app
    from commons_memory.auth import PRINCIPAL
    s = make_store(tmp_path)
    reg = s.register_agent("gem")
    app = build_app(s)
    tok = PRINCIPAL.set(reg["id"])
    try:
        c = TestClient(app)
        r = c.post("/agent/hook", json={"harness": "gemini", "event": "prompt",
                                        "payload": {"project": "x", "prompt": "hello there friend"}})
        assert r.status_code == 200 and r.json()["ok"]
        assert c.post("/agent/hook", json={"event": "bogus"}).status_code == 400
    finally:
        PRINCIPAL.reset(tok)


def test_rerank_cross_encoder_and_llm(tmp_path, monkeypatch):
    s = make_store(tmp_path)
    a = s.add_item("memory", "The deploy runbook lives in docs/ops.md")["id"]
    b = s.add_item("memory", "Deploy deploy deploy deploy")["id"]
    import commons_memory.rerank as RR

    class CE:
        def rerank(self, q, docs):
            return [10.0 if "runbook" in d else 0.0 for d in docs]
    monkeypatch.setattr(RR, "cross_encoder", lambda: CE())
    assert RR.rerank_ids(s, "where is the deploy runbook", [b, a], how="cross-encoder") == [a, b]
    from commons_memory.providers import ProviderLayer
    monkeypatch.setattr(ProviderLayer, "call_role", lambda self, role, p, max_tokens=0: "[1, 0]")
    assert RR.rerank_ids(s, "q", [b, a], how="llm") == [a, b]
    monkeypatch.setattr(ProviderLayer, "call_role", lambda self, role, p, max_tokens=0: None)
    assert RR.rerank_ids(s, "q", [b, a], how="llm") == [b, a]  # no reranker configured -> unchanged
    monkeypatch.setenv("COMMONS_RERANK", "cross-encoder")
    assert s.search("deploy runbook")["results"][0]["id"] == a


def test_bench_harness_tiny(tmp_path):
    from commons_memory import bench
    lme = [{"question_id": "q1", "question_type": "single-session-user", "question": "What is my dog's name?",
            "question_date": "2023/05/30 (Tue) 23:40", "answer": "Rex", "answer_session_ids": ["s2"],
            "haystack_session_ids": ["s1", "s2"], "haystack_dates": ["2023/05/01 (Mon) 10:00", "2023/05/02 (Tue) 10:00"],
            "haystack_sessions": [[{"role": "user", "content": "I like hiking in the alps."}],
                                  [{"role": "user", "content": "My dog Rex loves the beach."},
                                   {"role": "assistant", "content": "Rex sounds fun!"}]]},
           {"question_id": "q2_abs", "question_type": "x", "question": "?", "question_date": "", "answer": "",
            "answer_session_ids": [], "haystack_session_ids": [], "haystack_dates": [], "haystack_sessions": []}]
    p = tmp_path / "lme.json"
    p.write_text(json.dumps(lme))
    s = bench.longmemeval(str(p), str(tmp_path / "out"), embedder="hash", workers=1)
    assert s["n"] == 1 and s["metrics"]["any@1"] == 1
    loc = [{"sample_id": "c1", "conversation": {"speaker_a": "A", "speaker_b": "B",
            "session_1_date_time": "1:56 pm on 8 May, 2023",
            "session_1": [{"speaker": "A", "dia_id": "D1:1", "text": "I adopted a cat named Miso."},
                          {"speaker": "B", "dia_id": "D1:2", "text": "Nice! I went skiing."}]},
            "qa": [{"question": "What is the name of A's cat?", "answer": "Miso", "evidence": ["D1:1"], "category": 1}]}]
    p2 = tmp_path / "loc.json"
    p2.write_text(json.dumps(loc))
    s2 = bench.locomo(str(p2), str(tmp_path / "out"), embedder="hash", workers=1)
    assert s2["n"] == 1 and s2["metrics"]["any@1"] == 1 and s2["metrics"]["sess_any@1"] == 1
    assert (tmp_path / "out" / "locomo-hash.jsonl").exists()


def test_stop_without_activity_writes_nothing_and_never_repeats_a_line(tmp_path):
    from conftest import make_store
    from commons_memory.capture import Capture
    s = make_store(tmp_path)
    aid = s.ensure_agent("cc")
    cap = Capture(s)
    tail = "We decided to keep SQLite as the default store because Postgres was slower in every benchmark."
    before = s.q("SELECT COUNT(*) n FROM items")[0]["n"]
    for _ in range(3):  # three Stop events in a row carrying the same transcript tail, no prompts or tools
        cap.event(aid, "stop", {"session_id": "s1", "project": "p", "transcript_tail": tail}, harness="claude-code")
    items = s.q("SELECT kind, summary FROM items")
    assert not [i for i in items if (i["summary"] or "").startswith("Session (")], "empty-session summary written"
    assert s.q("SELECT COUNT(*) n FROM items")[0]["n"] - before <= 1, "the same captured line was stored more than once"


def test_harness_notices_are_not_user_prompts(tmp_path):
    from conftest import make_store
    from commons_memory.capture import Capture
    s = make_store(tmp_path)
    aid = s.ensure_agent("cc")
    Capture(s).event(aid, "prompt", {"session_id": "s2", "project": "p",
                                     "prompt": "<task-notification> background job finished </task-notification>"},
                     harness="claude-code")
    assert not s.q("SELECT 1 FROM observations WHERE session_key LIKE '%s2%' AND kind='prompt'")
