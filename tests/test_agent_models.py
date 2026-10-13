"""An agent's own model catalog (agy models, grok models) reaches Commons, and a newly launched model is announced."""
import asyncio
import sys

from commons_memory.providers import ProviderLayer
from commons_memory.quota import local_models


def test_a_new_model_in_an_agents_catalog_is_recorded_and_announced(store):
    pl, ag = ProviderLayer(store), store.ensure_agent("antigravity")
    first = pl.sync_agent_models(ag, [{"id": "gemini-3.8-flash-high", "name": "Gemini 3.8 Flash (High)"}, "gpt-oss-120b"])
    assert first["new"] == [] and not store.q("SELECT 1 FROM notifications")  # the first listing is the baseline
    out = pl.sync_agent_models(ag, [{"id": "gemini-3.8-flash-high", "name": "Gemini 3.8 Flash (High)"},
                                    {"id": "gemini-4-argon", "name": "Gemini 4 Argon"}])
    assert out["new"] == ["Gemini 4 Argon"]
    assert store.q("SELECT 1 FROM notifications WHERE title LIKE '%Gemini 4 Argon%'")
    by = {m["id"]: m for m in pl.models("antigravity")}
    assert by["antigravity/gemini-4-argon"]["is_new"] and by["antigravity/gemini-4-argon"]["agents"] == ["antigravity"]
    assert store.q("SELECT status FROM models WHERE id='antigravity/gpt-oss-120b'")[0]["status"] == "unavailable"
    assert pl.sync_agent_models(ag, ["gemini-3.8-flash-high", "gemini-4-argon"])["new"] == []  # announced once


def test_the_sync_reads_each_cli_and_reports_through_quota_report(store, tmp_path):
    (tmp_path / "agy.py").write_text('print("Fetching available models...")\nprint("gemini-4-argon\\tGemini 4 Argon")\n')
    (tmp_path / "grok.py").write_text('print("Available models:")\nprint("  * grok-4.7 (default)")\nprint("  - grok-4.6")\n')
    got = local_models({"antigravity": [sys.executable, str(tmp_path / "agy.py")],
                        "grok-build": [sys.executable, str(tmp_path / "grok.py")], "nope": ["no-such-cli-xyz"]})
    assert got == {"antigravity": [{"id": "gemini-4-argon", "name": "Gemini 4 Argon"}],
                   "grok-build": [{"id": "grok-4.7", "name": "grok-4.7"}, {"id": "grok-4.6", "name": "grok-4.6"}]}
    from commons_memory.server import build_server
    key = store.register_agent("antigravity")["api_key"]
    out = asyncio.run(build_server(store).call_tool("quota_report", {"windows": [], "models": got["antigravity"],
                                                                     "api_key": key}))
    assert '"models":1,' in out[1]["result"]
