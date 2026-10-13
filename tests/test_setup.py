import io
import json
import sys

from commons_memory import setup as SU


def test_one_command_sets_up_an_ai(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("COMMONS_HOME", raising=False)
    (tmp_path / ".gemini").mkdir()
    monkeypatch.setattr(sys, "stdin", io.StringIO("cmk_test_key_123\n"))
    rc = SU.run("gemini", "gem-agent", "http://hub:8770", {"wake", "import", "verify"})
    out = capsys.readouterr().out
    assert rc == 0, out
    key = tmp_path / ".commons" / "agents" / "gem-agent.key"
    assert key.read_text().strip() == "cmk_test_key_123" and oct(key.stat().st_mode)[-3:] == "600"
    cfg = json.loads((tmp_path / ".gemini" / "settings.json").read_text())
    assert cfg["mcpServers"]["commons"]["httpUrl"] == "http://hub:8770/mcp"
    assert "COMMONS_URL=http://hub:8770 COMMONS_AGENT=gem-agent" in json.dumps(cfg.get("hooks"))
    assert "start_work" in (tmp_path / ".gemini" / "GEMINI.md").read_text()
    assert "skipped (--no-wake)" in out and "cmk_test_key_123" not in out


def test_setup_supports_opencode(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("COMMONS_HOME", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO("cmk_oc_key_123\n"))
    assert SU.run("opencode", "oc-agent", "http://hub:8770", {"wake", "import", "verify"}) == 0
    cfg = json.loads((tmp_path / ".config" / "opencode" / "opencode.json").read_text())
    assert cfg["mcp"]["commons"] == {"type": "remote", "url": "http://hub:8770/mcp", "enabled": True,
                                     "headers": {"Authorization": "Bearer cmk_oc_key_123"}}
    assert "start_work" in (tmp_path / ".config" / "opencode" / "AGENTS.md").read_text()
    assert "turn() every turn" in capsys.readouterr().out
    from commons_memory.connect import bundle
    assert bundle("opencode", "http://hub:8770", "oc-agent")["blocks"][0]["id"] == "setup"
