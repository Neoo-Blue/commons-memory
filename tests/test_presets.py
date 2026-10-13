"""Roadmap: planner, summarizer and preference-extractor presets out of the box."""
from commons_memory import settings
from commons_memory.kb import KB
from commons_memory.providers import ProviderLayer


def test_light_roles_get_a_small_model_from_the_first_provider_key(store):
    pl = ProviderLayer(store)
    assert pl.role_model("kb_summarizer") is None  # no provider key: heuristics, as before
    pl.add_provider("openrouter", "openrouter", key_ref="env://OPENROUTER_API_KEY")
    assert pl.role_model("kb_summarizer") is None  # never a paid third-party router by default
    pl.add_provider("anthropic", "anthropic", key_ref="env://ANTHROPIC_API_KEY")
    for role in ("planner", "kb_summarizer", "pref_extractor"):
        assert pl.role_model(role) == "anthropic/claude-haiku-5-5"
    assert pl.role_model("librarian") is None and pl.role_model("planner", preset=False) is None
    assert pl.roles()["kb_summarizer"]["preset"] and not pl.roles()["kb_summarizer"]["explicit"]
    pl.set_role("kb_summarizer", "anthropic/claude-sonnet-5-5")
    assert pl.role_model("kb_summarizer") == "anthropic/claude-sonnet-5-5"  # your choice wins
    settings.put(store, "role_presets", False)
    assert pl.role_model("pref_extractor") is None


def test_an_unchanged_kb_section_is_summarized_once(store, monkeypatch):
    calls = []
    monkeypatch.setattr(ProviderLayer, "call_role", lambda self, role, prompt, max_tokens=1500: calls.append(role) or "Summary [m1].")
    kb = KB(store)
    assert kb._llm("p", "overview", "- a fact [m1]", ["m1"]).startswith("Summary [m1].")
    assert kb._llm("p", "overview", "- a fact [m1]", ["m1"]).startswith("Summary [m1].")
    kb._llm("p", "overview", "- a new fact [m2]", ["m2"])
    assert calls == ["kb_summarizer", "kb_summarizer"]


def test_qa_benchmark_batch_mode_sends_readers_then_judges_in_two_batches(monkeypatch, tmp_path):

    from commons_memory import bench
    sent = []

    class PL:
        def batch_submit(self, model, reqs):
            sent.append((model, reqs))
            return f"b{len(sent)}"

        def batch_results(self, model, bid, role=""):
            reqs = sent[int(bid[1:]) - 1][1]
            text = (lambda r: "Paris") if role == "reader" else (lambda r: '{"verdict": "CORRECT"}' if "Paris" in r["prompt"] else '{"verdict": "WRONG"}')
            return {"status": "ended", "results": {r["id"]: text(r) for r in reqs}, "cost": 0.01}
    items = [{"id": "q-1", "prompt": "Where?", "question": "Capital of France?", "gold": "Paris"},
             {"id": "q-2", "prompt": "Where?", "question": "Capital of Peru?", "gold": "Lima"}]
    got, cost = bench._qa_batched(PL(), "anthropic/m", "anthropic/j", items, log=lambda m: None, poll=0)
    assert [m for m, _ in sent] == ["anthropic/m", "anthropic/j"] and cost == 0.02
    assert got == [("Paris", True), ("Paris", True)]  # the judge saw the reader's answer for each id
    assert all("Generated answer: Paris" in r["prompt"] for r in sent[1][1])
