import json
import threading
import time
import urllib.error
import urllib.request

import pytest
from conftest import make_store

from commons_memory import connect as C
from commons_memory.dashboard import make_server
from commons_memory.store import CommonsError


@pytest.mark.parametrize("plat", list(C.PLATFORMS))
def test_bundle_every_platform(plat):
    b = C.bundle(plat, "http://192.168.50.111:8770", "grok", "cmk_SECRET", "proj")
    assert b["url"] == "http://192.168.50.111:8770/mcp"
    ids = [x["id"] for x in b["blocks"]]
    txt = {x["id"]: x["text"] for x in b["blocks"]}
    if plat in ("claude-code", "codex", "cursor", "gemini", "opencode"):  # v0.12: one command, every step on unless unticked
        assert ids == ["setup", "prompt"]
        setup = b["blocks"][0]
        assert f"commons setup --client {plat} --agent grok --url http://192.168.50.111:8770" in setup["text"]
        assert [x["id"] for x in setup["steps"]] == ["mcp", "instructions", "hooks", "wake", "import", "verify"]
        assert "cmk_SECRET" not in setup["text"]               # asked once at a hidden prompt, never in shell history
    else:
        assert ids == ["config", "prompt", "verify"]
        assert (plat == "docker" or "cmk_SECRET" in txt["config"]) and "cmk_SECRET" in txt["verify"]
        assert "initialize" in txt["verify"] and "whoami" in txt["verify"] and "echo OK" in txt["verify"]
    from commons_memory.server import INSTRUCTIONS
    assert INSTRUCTIONS in txt["prompt"]                           # the prompt is the master instructions: never stale
    assert "as agent `grok`" in txt["prompt"] and 'project="proj"' in txt["prompt"] and "cmk_SECRET" not in txt["prompt"]
    assert "http://192.168.50.111:8770/mcp" in txt["prompt"] and "/agent/recv" in txt["prompt"]
    assert len(b["troubleshooting"]) >= 4


def test_snippet_formats_and_placeholder():
    url = "http://10.0.0.5:8770/mcp"
    cur = json.loads(C.config_block("cursor", url)["text"])
    assert cur["mcpServers"]["commons"] == {"url": url, "headers": {"Authorization": f"Bearer {C.KEY_PLACEHOLDER}"}}
    gem = json.loads(C.config_block("gemini", url, key="cmk_a")["text"])
    assert gem["mcpServers"]["commons"]["httpUrl"] == url
    assert 'bearer_token_env_var = "COMMONS_API_KEY"' in C.config_block("codex", url)["text"]
    cc = C.config_block("claude-code", url, key="cmk_a")["text"]
    assert cc.startswith("claude mcp add --transport http") and "'Authorization: Bearer cmk_a'" in cc
    b = C.bundle("any", url)
    assert "cmk_" not in json.dumps(b) and not b["key_included"]
    assert C.norm_platform("claude") == "claude-code"
    with pytest.raises(CommonsError):
        C.norm_platform("nope")
    assert "troubleshooting" in C.render_text(b).lower()


def test_detect_url(monkeypatch):
    monkeypatch.delenv("COMMONS_PUBLIC_URL", raising=False)
    assert C.detect_url("192.168.50.111:8771", "0.0.0.0", 8770, False) == "http://192.168.50.111:8770/mcp"
    assert C.detect_url("nas.local:8771", "0.0.0.0", 8770, True) == "https://nas.local:8770/mcp"
    assert C.detect_url("localhost:8766", "127.0.0.1", 8765, False) == "http://localhost:8765/mcp"
    assert C.detect_url("100.64.1.2:8766", "127.0.0.1", 8765, False) == "http://127.0.0.1:8765/mcp"
    assert C.detect_url("whatever:1", "100.101.1.2", 8770, False) == "http://100.101.1.2:8770/mcp"
    monkeypatch.setenv("COMMONS_PUBLIC_URL", "https://commons.example.com/")
    assert C.detect_url("x", "0.0.0.0", 1) == "https://commons.example.com/mcp"


def test_key_creation_gate():
    assert C.key_creation_allowed("127.0.0.1", "127.0.0.1", False) == (True, "")
    assert C.key_creation_allowed("192.168.50.111", "192.168.50.20", False)[0]
    assert not C.key_creation_allowed("0.0.0.0", "192.168.1.2", False)[0]
    assert not C.key_creation_allowed("192.168.50.111", "8.8.8.8", False)[0]
    assert C.key_creation_allowed("0.0.0.0", "8.8.8.8", True, "admin")[0]
    assert not C.key_creation_allowed("0.0.0.0", "8.8.8.8", True, "viewer")[0]


def test_create_key_and_status(tmp_path):
    s = make_store(tmp_path)
    r = C.create_key(s, "bot1", "read", "p1", "cursor")
    assert r["api_key"].startswith("cmk_") and s.authenticate(r["api_key"])["name"] == "bot1"
    assert C.status(s, "bot1")["last_seen"] is None
    s.x("UPDATE agents SET last_seen=? WHERE name='bot1'", (time.time(),))
    assert C.status(s, "bot1")["connected"]
    with pytest.raises(CommonsError):
        C.create_key(s, "bad name", "write")
    with pytest.raises(CommonsError):
        C.create_key(s, "ok", "root")


def _srv(s, host="127.0.0.1", token=None):
    srv = make_server(s, host, 0, token)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _post(url, body, hdrs=None):
    # rejected requests are sent without a body: the server answers 401/403 before reading it, and macOS resets
    # the connection when unread request data is left in the socket
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else b"", {"X-Commons": "1", "Content-Type": "application/json", **(hdrs or {})})
    try:
        return 200, json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_dashboard_connect_api_tokenless_private(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONS_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("COMMONS_MCP_PORT", "8770")
    monkeypatch.delenv("COMMONS_PUBLIC_URL", raising=False)
    s = make_store(tmp_path)
    srv, base = _srv(s)
    try:
        b = json.loads(urllib.request.urlopen(base + "/api/connect?platform=cursor&name=x").read())
        assert b["detected_url"] == "http://127.0.0.1:8770/mcp" and b["can_create_key"]
        code, r = _post(base + "/api/connect/key", {"name": "newbot", "role": "write", "platform": "codex"})
        assert code == 200 and r["bundle"]["blocks"][0]["id"] == "setup" and r["api_key"] not in r["bundle"]["blocks"][0]["text"]
        st = json.loads(urllib.request.urlopen(base + "/api/connect_status?name=newbot").read())
        assert st["exists"] and st["last_seen"] is None
        html = urllib.request.urlopen(base + "/").read().decode()
        assert '"Connect"' in html and 'V["Connect"]' in html
    finally:
        srv.shutdown()


def test_dashboard_key_creation_gated(tmp_path, monkeypatch):
    s = make_store(tmp_path)
    from commons_memory.dashboard import add_dashboard_user
    viewer = add_dashboard_user(s, "v", "viewer")["token"]
    srv, base = _srv(s, token="admintok")
    try:
        assert _post(base + "/api/connect/key", None)[0] == 401
        assert _post(base + "/api/connect/key", None, {"Authorization": "Bearer " + viewer})[0] == 403
        code, r = _post(base + "/api/connect/key", {"name": "a"}, {"Authorization": "Bearer admintok"})
        assert code == 200 and r["api_key"]
    finally:
        srv.shutdown()
    # direct gate on the API (simulates a tokenless dashboard on a non-private bind)
    from commons_memory.dashboard import API
    from commons_memory.dashboard_v81 import CTX
    CTX.req = {"host": "x", "bind": "0.0.0.0", "client": "1.2.3.4", "has_auth": False, "role": "admin"}
    with pytest.raises(CommonsError):
        API(s).post("/api/connect/key", {"name": "b"})
    assert API(s).connect({"platform": "any"})["can_create_key"] is False
    assert s.get_agent("b") is None


def test_whoami_tool(tmp_path):
    import asyncio

    from commons_memory.server import build_server
    s = make_store(tmp_path)
    key = s.register_agent("w")["api_key"]
    mcp = build_server(s)
    out = asyncio.run(mcp.call_tool("whoami", {"api_key": key}))
    txt = json.dumps(out, default=str)
    assert "commons-ok" in txt and '\\"agent\\": \\"w\\"' in txt


def test_cli_connect_platform(tmp_path, capsys, monkeypatch):
    from commons_memory.cli import main
    db = str(tmp_path / "c.db")
    monkeypatch.setenv("COMMONS_URL", "http://10.1.1.1:8770/mcp")
    main(["--db", db, "connect", "gemini", "--name", "gem", "--new-key", "--json"])
    b = json.loads(capsys.readouterr().out)
    assert b["platform"] == "gemini" and b["key_included"] and b["url"] == "http://10.1.1.1:8770/mcp"
    main(["--db", db, "connect", "cursor"])
    out = capsys.readouterr().out
    assert "# Connect Cursor" in out and "commons setup --client cursor" in out
