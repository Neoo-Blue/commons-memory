"""Usage ledger: each agent's own records (Claude Code transcripts, peer bridge jobs) summed per day, model and effort."""
import json
import time
from pathlib import Path

import pytest

from commons_memory import ledger as L
from commons_memory.ids import now
from conftest import make_store

TODAY = time.strftime("%Y-%m-%d", time.gmtime())


def _reply(mid, model="claude-opus-5-5", effort="high", tin=10, tout=100, think=60, cr=5000, cw=300, side=False, cwd=""):
    return json.dumps({"type": "assistant", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "effort": effort,
                       "isSidechain": side, "cwd": cwd, "message": {"id": mid, "model": model, "usage": {
                           "input_tokens": tin, "output_tokens": tout, "output_tokens_details": {"thinking_tokens": think},
                           "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}}})


def test_claude_transcripts_count_each_reply_once_and_read_incrementally(tmp_path):
    root = tmp_path / "projects" / "-x"
    root.mkdir(parents=True)
    f = root / "s1.jsonl"
    lines = [_reply("m1"), _reply("m1"), _reply("m1"),                 # one reply, written once per content block
             _reply("m2", effort="medium"), _reply("m3", model="claude-haiku-4-5", side=True),
             json.dumps({"type": "user", "message": {"content": "hi"}}), _reply("m4", model="<synthetic>")]
    f.write_text("\n".join(lines) + "\n" + _reply("m5")[:40])        # a half-written last line
    st = {"files": {}, "jobs": {}, "totals": {}, "dirty": []}
    assert L.read_claude(st, "Claude", tmp_path / "projects") == 3
    hi = st["totals"][f"Claude|{TODAY}|claude-opus-5-5|high||claude-code"]
    assert hi[:6] == [1, 10, 100, 60, 5000, 300]
    assert f"Claude|{TODAY}|claude-haiku-4-5|high||claude-code subagent" in st["totals"]
    with open(f, "a") as fh:                                           # the half line completes, a new reply lands
        fh.write(_reply("m5")[40:] + "\n" + _reply("m6") + "\n")
    assert L.read_claude(st, "Claude", tmp_path / "projects") == 2
    assert st["totals"][f"Claude|{TODAY}|claude-opus-5-5|high||claude-code"][0] == 3
    assert L.read_claude(st, "Claude", tmp_path / "projects") == 0    # nothing new: nothing counted


def test_bridge_jobs_split_effort_from_the_model_and_skip_failed_or_running(tmp_path):
    jobs = tmp_path / "jobs"
    def job(name, result, meta=None):
        d = jobs / name
        d.mkdir(parents=True)
        (d / "result.json").write_text(json.dumps(result))
        (d / "meta.json").write_text(json.dumps(meta or {"started": time.strftime("%Y-%m-%d %H:%M:%S")}))
    job("a", {"status": "done", "model": "claude-opus-5-5-medium", "usage": {"input_tokens": 1000, "output_tokens": 200,
                                                                             "thinking_tokens": 50, "cache_read_tokens": 9}})
    job("b", {"status": "done", "model": "grok-4.7-build", "usage": {"input_tokens": 5, "output_tokens": 5}, "cost_usd": 0.42})
    job("c", {"status": "done", "model": "claude-opus-5-5-medium", "usage": {"input_tokens": 0, "output_tokens": 0}, "tokens": 0})
    job("d", {"status": "running", "model": "x", "usage": {"input_tokens": 9, "output_tokens": 9}})
    job("e", {"status": "done", "model": "grok-4.6-build", "usage": {"output_tokens": 150}})   # output only: input is 0, not -150
    job("f", {"status": "done", "model": "claude-opus-5-5-low", "usage": {"input_tokens": 3, "output_tokens": 3}},
        {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "effort": "high"})        # the model's own effort is what ran
    st = {"files": {}, "jobs": {}, "totals": {}, "dirty": []}
    assert L.read_jobs(st, "antigravity", jobs, "antigravity bridge") == 4
    assert st["totals"][f"antigravity|{TODAY}|grok-4.6-build|||antigravity bridge"][1] == 0
    assert f"antigravity|{TODAY}|claude-opus-5-5|low||antigravity bridge" in st["totals"]
    assert st["totals"][f"antigravity|{TODAY}|claude-opus-5-5|medium||antigravity bridge"][:5] == [1, 1000, 200, 50, 9]
    assert st["totals"][f"antigravity|{TODAY}|grok-4.7-build|||antigravity bridge"][6] == pytest.approx(0.42)
    assert "d" not in st["jobs"]["antigravity bridge"]                 # read again once it finishes
    assert L.read_jobs(st, "antigravity", jobs, "antigravity bridge") == 0


def test_collect_sends_dirty_totals_models_and_the_current_model(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONS_HOME", str(tmp_path / "ch"))
    root = Path.home() / ".claude" / "projects" / "-y"
    root.mkdir(parents=True, exist_ok=True)
    (root / "s.jsonl").write_text(_reply("z1") + "\n" + _reply("z2", model="claude-fable-5-1", tout=900) + "\n")
    st, send, models = L.collect()
    assert {r["model"] for r in send["Claude"]} == {"claude-opus-5-5", "claude-fable-5-1"}
    assert models["Claude"]["models"][0] == "claude-fable-5-1" and models["Claude"]["current"] == "claude-fable-5-1"
    L.mark_sent(st, "Claude")
    L.save(st)
    st2, send2, _ = L.collect()
    assert send2 == {} and st2["totals"]                               # sent once; totals kept for later days


def test_hub_stores_totals_not_increments_and_summarises_with_commons_calls(tmp_path):
    from commons_memory.dashboard import API
    from commons_memory.usage import Usage
    s = make_store(tmp_path, "led.db")
    aid = s.ensure_agent("Claude")
    row = {"day": TODAY, "model": "claude-opus-5-5", "effort": "high", "project": "commons-memory", "source": "claude-code",
           "calls": 10, "tokens_in": 100, "tokens_out": 1000, "thinking": 400, "cache_read": 90000, "cache_write": 2000}
    L.ingest(s, aid, [row])
    L.ingest(s, aid, [row | {"calls": 12, "tokens_out": 1500}])         # a re-send replaces, never adds
    hub = s.ensure_agent("provider:anthropic")
    Usage(s).report(hub, tokens_in=500, tokens_out=50, model="anthropic/claude-haiku-5-5", note="extract", cost=0.01)
    Usage(s).report(aid, tokens_in=7, tokens_out=7, model="claude-opus-5-5", note="step")   # Claude reports its own: skipped
    out = API(s).usage_ledger({"days": "7"})
    mine = [r for r in out["rows"] if r["agent"] == "Claude"]
    assert len(mine) == 1 and mine[0]["calls"] == 12 and mine[0]["tokens_out"] == 1500 and mine[0]["effort"] == "high"
    assert mine[0]["top_projects"][0][0] == "commons-memory"
    hub_rows = [r for r in out["rows"] if r["agent"] == "provider:anthropic"]
    assert hub_rows and hub_rows[0]["source"] == "commons" and hub_rows[0]["cost"] == pytest.approx(0.01)
    assert out["totals"]["calls"] == 13 and len(out["days"]) == 7
    assert API(s).usage_ledger({"agent": "nobody"})["rows"] == []        # an unknown agent shows nothing, not everyone


@pytest.mark.asyncio
async def test_quota_report_takes_a_ledger_and_the_current_model(tmp_path, monkeypatch):
    from commons_memory.server import build_server
    from commons_memory.store import Store
    monkeypatch.setenv("COMMONS_QUOTA_REPORTERS", "mac")
    s = Store(str(tmp_path / "q.db"), embedder="hash")
    s.ensure_agent("Claude")
    key = s.register_agent("mac")["api_key"]
    mcp = build_server(s)
    await mcp.call_tool("quota_report", {"windows": [], "for_agent": "Claude", "current_model": "claude-opus-5-5",
                                         "ledger": [{"day": TODAY, "model": "claude-opus-5-5", "calls": 2, "tokens_in": 1,
                                                     "tokens_out": 2}], "agent": "mac", "api_key": key})
    assert s.get_agent("Claude")["model"] == "claude-opus-5-5"
    assert L.summary(s, 3)["rows"][0]["calls"] == 2
