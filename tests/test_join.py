"""Join links: one line connects a machine's AIs (or a bot) and every one of them can be woken from then on."""
import io
import json

import pytest

from commons_memory import join as JN
from commons_memory.store import CommonsError


def _client(store):
    from starlette.testclient import TestClient

    from commons_memory.agent_http import build_app
    from commons_memory.auth import AuthMiddleware
    return TestClient(AuthMiddleware(build_app(store), store, resource_url="http://testserver", allow_anon_reads=False))


def test_a_code_trades_for_keys_up_to_its_limit_and_never_takes_an_existing_agent(store):
    j = JN.create(store, uses=2)
    a = JN.claim(store, j["code"], "claude-code-mac", "claude-code", "mac")
    assert a["api_key"].startswith("cmk_") and store.authenticate(a["api_key"])["name"] == "claude-code-mac"
    with pytest.raises(CommonsError, match="taken"):
        JN.claim(store, j["code"], "claude-code-mac", "claude-code", "mac")
    JN.claim(store, j["code"], "codex-mac", "codex", "mac")
    with pytest.raises(CommonsError, match="used up"):
        JN.claim(store, j["code"], "gemini-mac", "gemini", "mac")
    st = JN.status(store, j["code"])
    assert [x["name"] for x in st["joined"]] == ["claude-code-mac", "codex-mac"] and st["uses_left"] == 0
    old = JN.create(store, ttl=-1)
    with pytest.raises(CommonsError, match="expired"):
        JN.claim(store, old["code"], "late-agent")


def test_the_join_routes_need_no_key_but_the_code_and_everything_else_still_does(store):
    c = _client(store)
    j = JN.create(store)
    sh = c.get(f"/join/{j['code']}")
    assert sh.status_code == 200 and "commons join" in sh.text and j["code"] in sh.text and sh.text.startswith("#!/bin/sh")
    page = c.get(f"/join/{j['code']}/agent?as=grok")
    assert "--wake-cmd" in page.text and "You are already the Commons agent `grok`" in page.text and "--agent grok" in page.text
    r = c.post(f"/join/{j['code']}/claim", json={"name": "grok-bot", "client": "bot", "host": "box"})
    assert r.status_code == 200 and r.json()["name"] == "grok-bot"
    bad = c.get("/join/nope")
    assert bad.status_code == 404 and "exit 1" in bad.text and "doesn'\\''t" in bad.text     # an apostrophe can't break the sh
    assert c.get("/agent/presence").status_code == 401                                          # the rest still needs a key


def test_commons_join_connects_a_bot_and_always_installs_what_wakes_it(store, monkeypatch, tmp_path):
    monkeypatch.setenv("COMMONS_HOME", str(tmp_path / "ch"))
    j = JN.create(store)
    def fake_urlopen(req, timeout=30):
        b = json.loads(req.data)
        return io.BytesIO(json.dumps(JN.claim(store, j["code"], b["name"], b["client"], b["host"])).encode())
    ran = []
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: ran.append(cmd) or type("R", (), {"returncode": 0})())
    with pytest.raises(SystemExit, match="--wake-cmd is required"):
        JN.here("http://hub:8770", j["code"], name="grok-bot")
    assert JN.here("http://hub:8770/mcp", j["code"], name="grok-bot", wake_cmd="curl -s -X POST localhost:8765/wake") == 0
    cmd = ran[-1]
    assert cmd[1:4] == ["agent", "daemon", "--agent"] and "grok-bot" in cmd and "--install" in cmd
    assert cmd[cmd.index("--url") + 1] == "http://hub:8770" and cmd[cmd.index("--cmd") + 1].startswith("curl")
    keyf = tmp_path / "ch" / "agents" / "grok-bot.key"
    assert keyf.read_text().startswith("cmk_") and oct(keyf.stat().st_mode)[-3:] == "600"
    monkeypatch.setenv("COMMONS_API_KEY", "cmk_existing")                  # an agent that already has a key keeps it
    JN.here("http://hub:8770", j["code"], agent="grok", wake_cmd="grok -p")
    assert (tmp_path / "ch" / "agents" / "grok.key").read_text().strip() == "cmk_existing"
    assert [c["name"] for c in JN.status(store, j["code"])["joined"]] == ["grok-bot"]   # no second claim


def test_the_dashboard_makes_one_link_and_a_wake_fix_for_an_agent_it_cant_wake(store, monkeypatch):
    from commons_memory import dashboard_v81
    from commons_memory.dashboard import API, OWNER_ONLY
    monkeypatch.setattr(dashboard_v81.CTX, "req", {"bind": "127.0.0.1", "client": "127.0.0.1", "host": "127.0.0.1:8771"},
                        raising=False)                                    # a request from this machine, as the gate expects
    api = API(store)
    r = api.post("/api/connect/join", {})
    assert r["terminal"].endswith(f"/join/{r['code']} | sh") and r["bot"].startswith("Join my Commons: read ")
    assert r["connector"].endswith("/mcp")
    assert api.connect_join({"code": r["code"]})["joined"] == []
    store.ensure_agent("grok")
    w = api.post("/api/connect/wake", {"name": "grok"})
    assert "?as=grok" in w["bot"] and w["uses"] == 0                       # it keeps its key: the link can't mint one
    with pytest.raises(CommonsError, match="used up"):
        JN.claim(store, w["code"], "someone-else")
    assert {"/api/connect/join", "/api/connect/wake"} <= set(OWNER_ONLY)



def test_a_hostile_host_header_or_name_never_reaches_the_script(store):
    j = JN.create(store)
    with pytest.raises(CommonsError, match="unexpected characters"):
        JN.script(store, j["code"], 'http://x"; rm -rf ~; echo "')
    page = JN.agent_page(store, j["code"], "http://hub:8770", existing="grok`id`")
    assert "`grokid`" in page and "`id`" not in page
    r = JN.claim(store, j["code"], "bot;rm", client="bot$(id)", host="h`x`")
    assert r["name"] == "bot-rm" and JN.status(store, j["code"])["joined"][0]["client"] == "botid"



def test_a_code_never_takes_over_an_existing_identity(store, monkeypatch):
    monkeypatch.setenv("COMMONS_ADMIN_AGENTS", "cf:owner@example.com,root-agent")
    j = JN.create(store, uses=5)
    store.ensure_agent("user")                                            # keyless placeholders the hub made itself
    victim = store.ensure_agent("cf:owner@example.com")
    for name in ("user", "commons", victim, "root-agent", "librarian"):
        with pytest.raises(CommonsError, match="taken"):
            JN.claim(store, j["code"], name)
    store.ensure_agent("placeholder-bot")                                 # made when someone messaged that name
    with pytest.raises(CommonsError, match="taken"):
        JN.claim(store, j["code"], "placeholder-bot")
    assert store.q("SELECT key_hash FROM agents WHERE name='user'")[0]["key_hash"] is None
    JN.close(store, j["code"])
    with pytest.raises(CommonsError):
        JN.claim(store, j["code"], "fresh-name")


def test_the_hub_address_comes_from_the_code_not_the_request(store):
    from commons_memory.dashboard_v81 import join_lines
    j = JN.create(store, hub="http://192.168.50.111:8770/mcp")
    assert 'HUB="http://192.168.50.111:8770"' in JN.script(store, j["code"], "http://evil.example:8770")
    assert join_lines("http://192.168.50.111:8770", "c")["plain_http"] is True
    assert join_lines("http://100.101.1.2:8770", "c")["plain_http"] is False and join_lines("https://hub.example", "c")["plain_http"] is False
