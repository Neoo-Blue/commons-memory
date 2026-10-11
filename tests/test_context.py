
import pytest

from commons_memory.contextpacks import ContextStore, count_tokens
from commons_memory.orchestrator import Orchestrator
from commons_memory.providers import ProviderLayer
from commons_memory.store import CommonsError

BIG = "def handler(event):\n    return process(event)\n" * 200


def _pack(cs, **kw):
    return cs.save("checkout-refactor", "Refactoring checkout into services; payments done, tax next.",
                   {"goal": "Split checkout", "state": "payments extracted", "decisions": ["Use Stripe", "Keep SQLite"],
                    "open_threads": ["tax rounding"], "next_steps": ["extract tax"]},
                   {"notes": "Tax rounding uses banker's rounding.", "code: handler.py": BIG},
                   [{"id": "dec_123", "note": "stripe decision"}, {"path": "src/tax.py", "hash": "abc123"}], **kw)


def test_save_layers_dedupe_and_delta(store):
    cs = ContextStore(store)
    a = _pack(cs)
    assert a["sections"] == 2 and a["tokens"]["L2"] > a["tokens"]["L1"] > a["tokens"]["L0"] > 0
    b = cs.save("checkout-refactor", "Tax extracted; next: invoices.", sections={"notes": "Tax done; invoices next."},
                delta=True)
    assert b["parent"] == a["pack_id"] and b["delta"] and b["sections"] == 2
    assert b["reused_chars"] >= len(BIG) and b["new_chars"] < 200  # big code section stored once
    assert store.q("SELECT COUNT(*) c FROM context_blobs WHERE content=?", (BIG,))[0]["c"] == 1
    d = cs.diff(b["pack_id"])
    assert d["changed"][0]["title"] == "notes" and "+Tax done" in d["changed"][0]["diff"]
    with pytest.raises(CommonsError):
        cs.save("x", "")


def test_load_layers_budget_fit_and_secrets(store):
    cs = ContextStore(store)
    p = _pack(cs)["pack_id"]
    l0 = cs.load(p, "L0")
    l1 = cs.load(p, "L1")
    assert l0["tokens"] < l1["tokens"] and "## Brief (L1)" in l1["prefix"] and "tax rounding" in l1["text"]
    l2 = cs.load(p, "L2", max_tokens=l1["tokens"] + 60)
    assert "notes" in l2["included"] and "code: handler.py" in l2["omitted"]
    l3 = cs.load(p, "L3", max_tokens=100000)
    assert "src/tax.py" in l3["text"] and "dec_123" in l3["text"]
    assert cs.load(p, "L1")["prefix"] == l1["prefix"]  # byte-stable prefix -> provider cache hits
    small = cs.fit(p, "ollama/tiny-model-4k", reserve=0, share=0.005)
    assert small["layer"] in ("L0", "L1")
    big = cs.fit(p, "anthropic/claude-x", reserve=1000)
    assert big["layer"] == "L3" and big["context_window"] == 200000
    s = cs.save("s", "gist with key sk-ant-api03-" + "Z" * 40)
    assert "ZZZZZZ" not in cs.load(s["pack_id"], "L0")["text"]
    assert cs.savings()["tokens_saved_by_layering"] > 0
    assert count_tokens("hello world " * 100, "claude") > count_tokens("hello world " * 100, "gpt-4o") * 0.9


def test_job_passes_pack_id_and_prompt_cache(store):
    cs = ContextStore(store)
    p = _pack(cs)["pack_id"]
    pl = ProviderLayer(store)
    pl.add_provider("mock", "mock", config={"models": [{"model": "m", "quality": 1, "cost_in": 3, "cost_out": 15}]})
    pl.refresh_models()
    a = store.ensure_agent("seat")
    o = Orchestrator(store)
    job = o.submit("Extract invoices\n- part one\n- part two", pattern="parallel", agent_id=store.ensure_agent("u"),
                   context_pack=p)
    pk = o.claim(a)
    assert pk["context"]["pack_id"] == p and pk["context"]["prefix"].startswith("# Shared context: checkout-refactor")
    from commons_memory.worker import render_prompt
    pr1 = render_prompt(pk)
    assert pr1.startswith(pk["context"]["prefix"])
    r1 = pl.chat("mock/m", "task one", cache_prefix=pk["context"]["prefix"])
    r2 = pl.chat("mock/m", "task two", cache_prefix=pk["context"]["prefix"])
    assert r1["cached_tokens"] == 0 and r2["cached_tokens"] > 0 and r2["cost"] < r1["cost"] + 1e-9
    assert store.q("SELECT SUM(cached_tokens) c FROM usage")[0]["c"] > 0
    assert job["job_id"]
