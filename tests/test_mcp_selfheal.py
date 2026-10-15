"""Set once, stays working: Commons checks its own MCP registration at every session start and puts it back."""
import json
import subprocess
import sys
from pathlib import Path

from commons_memory import setup as SU

URL = "http://hub.local:8770"


def _hooks(home: Path, wrapper="/opt/bin/commons-hook"):
    (home / ".claude").mkdir(exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": f"{wrapper} hook claude-code SessionStart"}]}]}}))


def test_a_missing_registration_is_put_back_with_a_key_free_helper(monkeypatch):
    home = Path.home()
    _hooks(home)
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"grok": {"type": "stdio"}}}))
    calls = []
    monkeypatch.setattr(SU.shutil, "which", lambda *a, **k: "/usr/bin/claude")
    monkeypatch.setattr(SU.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    note = SU.ensure_mcp("claude-code", URL)
    assert "re-registered" in note and len(calls) == 1
    cmd = calls[0]
    assert cmd[:6] == ["/usr/bin/claude", "mcp", "add-json", "--scope", "user", "commons"]
    entry = json.loads(cmd[6])
    assert entry == {"type": "http", "url": URL + "/mcp", "headersHelper": "/opt/bin/commons-hook mcp-auth"}
    assert "Bearer" not in cmd[6]                                       # no key in the client's config
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"commons": entry}}))
    assert SU.ensure_mcp("claude-code", URL) == "" and len(calls) == 1  # registered: nothing to do
    assert SU.ensure_mcp("codex", URL) == ""                            # other clients: doctor handles them


def test_mcp_auth_prints_the_header_from_the_key_source(monkeypatch):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "COMMONS_API_KEY": "k-123"}
    r = subprocess.run([sys.executable, "-m", "commons_memory", "mcp-auth"], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and json.loads(r.stdout) == {"Authorization": "Bearer k-123"}
    r = subprocess.run([sys.executable, "-m", "commons_memory", "mcp-auth"], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())})
    assert r.returncode == 1 and not r.stdout                           # no key: fail loudly, print nothing
