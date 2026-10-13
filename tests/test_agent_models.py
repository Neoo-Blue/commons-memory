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


def test_commons_decides_when_an_agents_tool_updates(store, monkeypatch):
    from commons_memory import agentcli as AC
    from commons_memory import settings
    grok, agy = store.ensure_agent("grok-build"), store.ensure_agent("antigravity")
    assert AC.report(store, grok, {"version": "1.0.52", "latest": "1.0.52", "can_check": True}) == {"update": False}
    assert AC.report(store, grok, {"version": "1.0.52", "latest": "1.0.53", "can_check": True})["update"]  # newer out
    AC.report(store, grok, {"version": "1.0.53", "result": "updated", "ok": True})
    assert store.q("SELECT 1 FROM notifications WHERE title LIKE '%grok-build updated 1.0.52 → 1.0.53%'")
    assert not AC.report(store, grok, {"version": "1.0.53", "latest": "1.0.53", "can_check": True})["update"]
    assert AC.report(store, agy, {"version": "1.3.4", "can_check": False})["update"]  # can't check: try once a day
    AC.report(store, agy, {"version": "1.3.4", "result": "already on the latest version", "ok": True})
    assert not AC.report(store, agy, {"version": "1.3.4", "can_check": False})["update"]
    AC.request(store, "antigravity")  # the owner pressed Update now
    assert AC.report(store, agy, {"version": "1.3.4", "can_check": False})["update"]
    AC.report(store, agy, {"version": "1.3.4", "result": "network down", "ok": False})
    assert store.q("SELECT 1 FROM notifications WHERE title LIKE '%antigravity: updating its tool failed%'")
    settings.put(store, "agent_updates", "notify")
    assert not AC.report(store, grok, {"version": "1.0.53", "latest": "1.0.60", "can_check": True})["update"]
    assert store.q("SELECT 1 FROM notifications WHERE title LIKE '%grok-build 1.0.60 is out%'")
    assert {c["agent"]: c["update_available"] for c in AC.listing(store)} == {"antigravity": False, "grok-build": True}


def test_this_machine_reads_versions_and_runs_updaters(tmp_path):
    from commons_memory import quota as Q
    (tmp_path / "v.py").write_text('print("tool 2.4.1 (stable)")\n')
    (tmp_path / "chk.py").write_text('print(\'{"currentVersion":"2.4.1","latestVersion":"2.5.0"}\')\n')
    (tmp_path / "up.py").write_text('print("updated to 2.5.0")\n')
    py = sys.executable
    got = Q.local_clis({"claude": {"five_hour": ["my-agent"]}}, {
        "grok-build": {"version": [py, str(tmp_path / "v.py")], "check": [py, str(tmp_path / "chk.py")],
                       "update": [py, str(tmp_path / "up.py")]},
        "claude": {"version": [py, str(tmp_path / "v.py")], "update": [py, str(tmp_path / "up.py")]},
        "missing": {"version": ["no-such-cli-xyz", "--version"], "update": ["no-such-cli-xyz"]}})
    assert got["grok-build"]["latest"] == "2.5.0" and got["grok-build"]["version"] == "2.4.1"
    assert got["my-agent"]["version"] == "2.4.1" and not got["my-agent"]["can_check"]  # claude -> its mapped agent
    assert "missing" not in got
    assert Q.run_update(got["grok-build"]["update"]) == (True, "updated to 2.5.0")
