"""quota sync on a machine with no aiusage database, and the 10-minute install on each OS."""
import io
import json
import subprocess
import sys

import pytest

from commons_memory import setup as SU
from commons_memory.cli import main
from commons_memory.store import CommonsError


class Hub:
    def __init__(self, url, api_key):
        self.url, self.key = url, api_key
        self.calls.append((url, api_key))

    calls: list = []
    seen: list = []
    refuse: set = set()

    def _call(self, tool, args):
        body = args["args"]
        self.seen.append((self.key, body))
        if body.get("for_agent") in self.refuse:
            raise RuntimeError("only agents in COMMONS_QUOTA_REPORTERS may report quota for other agents")
        return json.dumps({"models": {"new": []}, "cli": {"update": False}})


def _patch_local(monkeypatch, models, clis, windows=None):
    monkeypatch.setattr("commons_memory.quota.local_models", lambda cmds=None: models)
    monkeypatch.setattr("commons_memory.quota.local_clis", lambda mapping, cmds=None: clis)
    if windows is not None:
        monkeypatch.setattr("commons_memory.quota.from_aiusage", lambda db_path=None, mapping=None: windows)
    monkeypatch.setattr("commons_memory.worker.McpClient", Hub)


def test_sync_without_aiusage_db_still_reports_models_and_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("COMMONS_API_KEY", "cmk_from_env")
    key = tmp_path / "agent.key"
    key.write_text("cmk_from_file\n")
    Hub.calls, Hub.seen, Hub.refuse = [], [], set()
    _patch_local(monkeypatch,
                 {"antigravity": [{"id": "gemini-4-argon", "name": "Gemini 4 Argon"}]},
                 {"antigravity": {"version": "1.2.3", "can_check": False, "update": ["agy", "update"]}})
    # the real from_aiusage raises CommonsError when ~/.aiusage/usage.db is missing
    with pytest.raises(CommonsError):
        from commons_memory.quota import from_aiusage
        from_aiusage()
    rc = main(["quota", "sync", "--url", "http://hub:8770/mcp", "--api-key-file", str(key)])
    out, err = capsys.readouterr()
    assert rc == 0, err
    synced = json.loads(out)["synced"]["antigravity"]
    assert "1 models" in synced and "v1.2.3" in synced
    assert Hub.seen[0][0] == "cmk_from_file"  # the file wins over COMMONS_API_KEY
    body = Hub.seen[0][1]
    assert body["windows"] == [] and body["for_agent"] == "antigravity"
    assert body["models"] == [{"id": "gemini-4-argon", "name": "Gemini 4 Argon"}]
    assert body["cli"] == {"version": "1.2.3", "can_check": False}
    assert "no aiusage" not in err


def test_one_refused_agent_does_not_stop_the_others(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("COMMONS_API_KEY", "cmk_reporter")
    Hub.calls, Hub.seen, Hub.refuse = [], [], {"antigravity"}
    _patch_local(
        monkeypatch,
        {"antigravity": [{"id": "gemini-4", "name": "Gemini 4"}],
         "grok-build": [{"id": "grok-4.7", "name": "grok-4.7"}]},
        {"antigravity": {"version": "1.0.0", "can_check": False, "update": ["agy", "update"]},
         "grok-build": {"version": "2.0.0", "can_check": False, "update": ["grok", "update"]}},
        windows={"antigravity": [{"window": "antigravity:weekly", "used_pct": 3, "resets_at": "2026-10-20T00:00:00Z"}],
                 "grok-build": [{"window": "grok:weekly_all", "used_pct": 12, "resets_at": "2026-10-20T00:00:00Z"}]},
    )
    rc = main(["quota", "sync", "--url", "http://hub:8770/mcp"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    # antigravity sorts first, so a raised report has to be caught for grok-build to be sent
    assert out["synced"]["antigravity"] == [
        "error: only agents in COMMONS_QUOTA_REPORTERS may report quota for other agents"]
    grok = out["synced"]["grok-build"]
    assert "grok:weekly_all" in grok and "1 models" in grok and "v2.0.0" in grok
    assert [body["for_agent"] for _key, body in Hub.seen] == ["antigravity", "grok-build"]


def test_old_hub_retry_reports_windows_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("COMMONS_API_KEY", "cmk")
    calls = []

    class Old:
        def __init__(self, url, api_key):
            pass

        def _call(self, tool, args):
            body = args["args"]
            calls.append(body)
            if "models" in body or "cli" in body:
                raise RuntimeError("quota_report() got an unexpected keyword argument 'models'")
            return "{}"

    monkeypatch.setattr("commons_memory.worker.McpClient", Old)
    monkeypatch.setattr("commons_memory.quota.local_models", lambda cmds=None: {
        "antigravity": [{"id": "gemini-4", "name": "Gemini 4"}]})
    monkeypatch.setattr("commons_memory.quota.local_clis", lambda mapping, cmds=None: {
        "antigravity": {"version": "1.2.3", "can_check": False, "update": ["agy", "update"]}})
    monkeypatch.setattr("commons_memory.quota.from_aiusage", lambda db_path=None, mapping=None: {
        "antigravity": [{"window": "antigravity:weekly", "used_pct": 1, "resets_at": "2026-10-20T00:00:00Z"}]})
    rc = main(["quota", "sync", "--url", "http://hub:8770/mcp"])
    line = json.loads(capsys.readouterr().out)["synced"]["antigravity"]
    assert rc == 0
    assert line == ["antigravity:weekly", "windows only"]
    assert "models" in calls[0] and "cli" in calls[0]
    assert "models" not in calls[1] and "cli" not in calls[1]


def test_a_transient_report_error_is_not_retried_as_success(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("COMMONS_API_KEY", "cmk")
    calls = []

    class Down:
        def __init__(self, url, api_key):
            pass

        def _call(self, tool, args):
            calls.append(args["args"])
            raise RuntimeError("connection reset")

    monkeypatch.setattr("commons_memory.worker.McpClient", Down)
    monkeypatch.setattr("commons_memory.quota.local_models", lambda cmds=None: {
        "antigravity": [{"id": "gemini-4", "name": "Gemini 4"}]})
    monkeypatch.setattr("commons_memory.quota.local_clis", lambda mapping, cmds=None: {
        "antigravity": {"version": "1.2.3", "can_check": False, "update": ["agy", "update"]}})
    monkeypatch.setattr("commons_memory.quota.from_aiusage", lambda db_path=None, mapping=None: {
        "antigravity": [{"window": "antigravity:weekly", "used_pct": 1, "resets_at": "2026-10-20T00:00:00Z"}]})
    rc = main(["quota", "sync", "--url", "http://hub:8770/mcp"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["synced"]["antigravity"] == ["error: connection reset"]
    assert len(calls) == 1


def _install(monkeypatch, tmp_path, platform):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", platform)
    key = tmp_path / "agent.key"
    key.write_text("cmk_x\n")
    ran = []

    def fake_run(cmd, **kwargs):
        ran.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("commons_memory.cli_v7.subprocess.run", fake_run)
    rc = main(["quota", "install", "--url", "http://hub:8770/mcp", "--api-key-file", str(key)])
    return rc, ran, key


def test_quota_install_writes_launchd_plist(tmp_path, monkeypatch, capsys):
    rc, ran, key = _install(monkeypatch, tmp_path, "darwin")
    out = capsys.readouterr().out
    assert rc == 0, out
    plist = tmp_path / "Library" / "LaunchAgents" / "dev.commons.sync.plist"
    text = plist.read_text()
    assert "<key>Label</key><string>dev.commons.sync</string>" in text
    assert "<key>RunAtLoad</key><true/>" in text
    assert "<key>StartInterval</key><integer>600</integer>" in text
    assert "<string>quota</string><string>sync</string>" in text
    assert "--url" in text and "http://hub:8770/mcp" in text
    assert "--api-key-file" in text and str(key.resolve()) in text
    log = str(tmp_path / ".commons" / "logs" / "sync.log")
    assert log in text and text.count(log) == 2  # stdout and stderr
    assert "KeepAlive" not in text
    assert ran == [["launchctl", "unload", str(plist)], ["launchctl", "load", "-w", str(plist)]]
    assert "every 10 minutes" in out


def test_quota_install_writes_systemd_units(tmp_path, monkeypatch, capsys):
    rc, ran, key = _install(monkeypatch, tmp_path, "linux")
    assert rc == 0, capsys.readouterr().out
    folder = tmp_path / ".config" / "systemd" / "user"
    service, timer = (folder / "commons-sync.service").read_text(), (folder / "commons-sync.timer").read_text()
    assert "Type=oneshot" in service
    assert "quota sync --url http://hub:8770/mcp --api-key-file " + str(key.resolve()) in service
    assert f"append:{tmp_path / '.commons' / 'logs' / 'sync.log'}" in service
    assert "OnBootSec=2min" in timer and "OnUnitActiveSec=10min" in timer
    assert ran == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "commons-sync.timer"],
        ["systemctl", "--user", "start", "commons-sync.service"],
    ]


def test_quota_install_saves_the_env_key_for_the_job(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("COMMONS_HOME", raising=False)
    monkeypatch.setenv("COMMONS_API_KEY", "cmk_env_secret")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("commons_memory.cli_v7.subprocess.run",
                        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0))
    rc = main(["quota", "install", "--url", "http://hub:8770/mcp"])
    err = capsys.readouterr().err
    assert rc == 0, err
    keyfile = tmp_path / ".commons" / "sync.key"
    assert keyfile.read_text().strip() == "cmk_env_secret"
    assert oct(keyfile.stat().st_mode)[-3:] == "600"
    plist = (tmp_path / "Library" / "LaunchAgents" / "dev.commons.sync.plist").read_text()
    assert "--api-key-file" in plist and str(keyfile.resolve()) in plist
    assert "cmk_env_secret" not in plist


def test_quota_install_without_a_key_does_not_write_a_unit(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("COMMONS_HOME", raising=False)
    monkeypatch.delenv("COMMONS_API_KEY", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    rc = main(["quota", "install", "--url", "http://hub:8770/mcp"])
    assert rc == 2
    assert "COMMONS_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "Library" / "LaunchAgents" / "dev.commons.sync.plist").exists()


def test_quota_install_prints_the_windows_command(tmp_path, monkeypatch, capsys):
    rc, ran, _key = _install(monkeypatch, tmp_path, "win32")
    out, err = capsys.readouterr()
    assert rc == 0 and ran == []
    assert "schtasks /Create /SC MINUTE /MO 10" in out
    assert "quota sync" in out and "http://hub:8770/mcp" in out and "--api-key-file" in out
    assert "Windows" in err


def test_setup_steps_include_sync_and_no_sync_skips(tmp_path, monkeypatch, capsys):
    assert SU.STEPS[SU.STEPS.index("wake") + 1] == "sync"
    assert SU.LABELS["sync"]
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("COMMONS_HOME", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO("cmk_test_key_123\n"))
    (tmp_path / ".gemini").mkdir()
    rc = main(["setup", "--client", "gemini", "--agent", "gem", "--url", "http://hub:8770",
               "--no-wake", "--no-sync", "--no-import", "--no-verify"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "skipped (--no-sync)" in out
    assert "keep this AI's models and tool version current" in out


def test_doctor_still_runs(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("COMMONS_API_KEY", raising=False)
    monkeypatch.delenv("COMMONS_HOME", raising=False)
    def down(*_a, **_k):
        raise OSError("down")

    monkeypatch.setattr("urllib.request.urlopen", down)
    rc = SU.doctor("gemini", "gem", "http://127.0.0.1:9")
    out = capsys.readouterr().out
    assert rc == 1 and "commons doctor:" in out and "FAIL" in out


def test_quota_install_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["quota", "install", "--help"])
    assert exc.value.code == 0
    assert "--api-key-file" in capsys.readouterr().out
