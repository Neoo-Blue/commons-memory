"""Re-filing: imported and folder notes checked against the real projects, proposals grouped, moved with Undo."""
import json

from commons_memory import projects as P
from commons_memory import refile as R
from commons_memory.ids import now
from conftest import make_store

DAY = 86400


def _seed(s):
    a = s.ensure_agent("claude-code")
    t = now() - 5 * DAY
    for d in range(3):  # live notes give each project its identity
        s.add_item("decision", f"Poise meeting recap emails go out after notetaker joins {d}", project="cooli-phrase", agent_id=a, created_at=t + d * DAY)
        s.add_item("decision", f"Pool bot aims with a 3% cut tolerance on the 9-ball table {d}", project="soul-pool", agent_id=a, created_at=t + d * DAY)
    wrong = [s.add_item("memory", f"Voiceprint consent decides whether Poise remembers speakers, case {i}", project="soul-pool",
                        agent_id=a, meta={"imported": True, "src": f"claude-mem:obs:{i}"})["id"] for i in range(4)]
    right = s.add_item("memory", "Break shot speed tuned for the pool table rails", project="soul-pool", agent_id=a,
                       meta={"imported": True})["id"]
    live = s.add_item("memory", "Poise deploy notes written while in the pool repo", project="soul-pool", agent_id=a)["id"]
    home = s.add_item("memory", "Poise billing notices only go to verified addresses", project="home", agent_id=a)["id"]
    return wrong, right, live, home


class FakePL:
    """Moves every note that mentions Poise to the catalog code whose title is cooli-phrase."""
    def __init__(self, s): self.s = s
    def role_model(self, role, preset=True): return "mock/m"
    def _p(self, provider): return {"kind": "mock"}
    def chat(self, model, prompt, system="", cache_prefix="", max_tokens=0, role="", schema=None, **kw):
        code = [ln.split()[0] for ln in cache_prefix.splitlines() if " cooli-phrase" in ln][0]
        moves = [{"id": ln[1:ln.index("]")], "to": code, "confidence": "high"} for ln in prompt.splitlines()
                 if ln.startswith("[") and "Poise" in ln]
        return {"text": json.dumps({"moves": moves}), "cost": 0.01}


def test_candidates_are_imported_and_folder_notes_only_and_moves_undo(tmp_path, monkeypatch):
    import commons_memory.providers as prov
    s = make_store(tmp_path, "rf.db")
    wrong, right, live, home = _seed(s)
    est = R.estimate(s)
    assert est["notes"] == 6 and est["batches"] == 1                      # 4 wrong + 1 right import + 1 home note
    monkeypatch.setattr(prov, "ProviderLayer", FakePL)
    r = R.run(s)
    assert r["proposed"] == 5                                              # never the live note, never the pool note
    flows = {f["key"]: f for f in R.suggestions(s)}
    sp = [f for f in flows.values() if f["from_title"] == "soul-pool"][0]
    assert sp["count"] == 4 and sp["to_title"] == "cooli-phrase" and sp["samples"]
    done = R.apply(s, sp["key"])
    assert done["moved"] == 4
    assert {r["project_id"] for r in s.q(f"SELECT project_id FROM items WHERE id IN ({','.join('?' * 4)})", wrong)} == \
        {s.project_id("cooli-phrase")}
    assert s.q("SELECT project_id FROM items WHERE id=?", (live,))[0]["project_id"] == s.project_id("soul-pool")
    R.undo(s, done["undo"]["key"], done["undo"]["at"])
    assert {r["project_id"] for r in s.q(f"SELECT project_id FROM items WHERE id IN ({','.join('?' * 4)})", wrong)} == \
        {s.project_id("soul-pool")}
    hm = [f for f in R.suggestions(s) if f["from_title"] == "home"][0]
    R.dismiss(s, hm["key"])
    assert not [f for f in R.suggestions(s) if f["from_title"] == "home"]
    assert R.estimate(s)["notes"] == 1                                     # decided notes are not checked again


def test_start_is_owner_only_and_runs_once(tmp_path, monkeypatch):
    from commons_memory.dashboard import OWNER_ONLY
    assert any("/api/refile/start".startswith(p) for p in OWNER_ONLY)
    s = make_store(tmp_path, "rf2.db")
    gate, calls = __import__("threading").Event(), []
    monkeypatch.setattr(R, "run", lambda st, **kw: (calls.append(1), gate.wait(2), {"proposed": 0})[2])
    R.run_async(s)
    second = R.run_async(s)
    gate.set()
    __import__("time").sleep(0.2)
    assert second["status"] == "running" and len(calls) == 1


def test_a_description_lets_notes_leave_for_unsorted(tmp_path, monkeypatch):
    import commons_memory.providers as prov
    s = make_store(tmp_path, "rf3.db")
    wrong, right, live, home = _seed(s)
    stray = s.add_item("memory", "Quarterly tax filing reminder for the accountant", project="soul-pool",
                       meta={"imported": True})["id"]
    sp = [e for e in P.listing(s)["projects"] if e["name"] == "soul-pool"][0]
    P.update(s, sp["id"], description="The pool-playing agent: only notes about playing pool")
    assert P.detail(s, "soul-pool")["description"].startswith("The pool-playing agent")
    seen = {}

    class DescPL(FakePL):
        def chat(self, model, prompt, system="", cache_prefix="", **kw):
            seen["prefix"], seen["system"] = cache_prefix, system
            out = json.loads(super().chat(model, prompt, system=system, cache_prefix=cache_prefix, **kw)["text"])
            out["moves"] += [{"id": ln[1:ln.index("]")], "to": "NONE", "confidence": "high"} for ln in prompt.splitlines()
                             if ln.startswith("[") and "tax filing" in ln]
            return {"text": json.dumps(out), "cost": 0.0}
    monkeypatch.setattr(prov, "ProviderLayer", DescPL)
    R.run(s)
    assert "is: The pool-playing agent" in seen["prefix"] and "NONE" in seen["system"]
    flow = [f for f in R.suggestions(s) if f["to_title"] == "unsorted"][0]
    R.apply(s, flow["key"])
    assert s.q("SELECT project_id FROM items WHERE id=?", (stray,))[0]["project_id"] == s.project_id("unsorted")
    assert "unsorted" in [e["name"] for e in P.listing(s)["folders"]]


def test_a_described_projects_live_notes_are_checked_too(tmp_path):
    s = make_store(tmp_path, "rf4.db")
    wrong, right, live, home = _seed(s)
    before = R.estimate(s)["notes"]
    sp = [e for e in P.listing(s)["projects"] if e["name"] == "soul-pool"][0]
    P.update(s, sp["id"], description="The pool-playing agent")
    assert R.estimate(s)["notes"] > before                 # its live notes (incl. the Poise one written in its folder)
