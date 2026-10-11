import json
import sys
from pathlib import Path

import pytest

from commons_memory.mcphub import Hub, classify, load_catalog
from commons_memory.store import CommonsError

UP = str(Path(__file__).parent / "mock_upstream_mcp.py")


@pytest.fixture
def hub(store, monkeypatch):
    monkeypatch.setenv("UPSTREAM_TOKEN", "tok_live_123")
    h = Hub(store)
    h.add("up", command=sys.executable, args=[UP], env_refs={"UPSTREAM_TOKEN": "env://UPSTREAM_TOKEN"})
    return h


def test_register_discover_search_describe(hub):
    s = hub.servers()[0]
    assert s["status"] == "up" and s["tools"] == 4 and s["env_refs"] == {"UPSTREAM_TOKEN": "env://UPSTREAM_TOKEN"}
    txt = hub.search("weather")["text"]
    assert txt.startswith("up__get_weather [read]")
    assert "delete_everything [destructive]" in hub.search("delete")["text"]
    assert hub.describe("up__create_note")["input_schema"]["properties"]["text"]
    with pytest.raises(CommonsError):
        hub.add("bad", url="https://x.example/mcp", header_refs={"Authorization": "Bearer sk-raw"})


def test_call_cache_policy_approval_untrusted(hub, store):
    a = store.ensure_agent("claude-code")
    r = hub.call("up__get_weather", {"city": "Oslo"}, a)
    assert r["status"] == "ok" and "Oslo: sunny" in r["result"] and r["result"].startswith('<untrusted source="mcp:up/get_weather">')
    assert hub.call("up__get_weather", {"city": "Oslo"}, a)["cached"]
    assert not hub.call("up__create_note", {"text": "hi"}, a)["cached"]
    pend = hub.call("up__delete_everything", {"confirm": True}, a)
    assert pend["status"] == "approval_required"
    assert hub.call("up__delete_everything", {"confirm": True}, a, approval_id="apr_fake")["status"] == "approval_required"
    pois = hub.call("up__poisoned", {}, a)["result"]
    assert "sk-ant-api03-BBBB" not in pois and pois.count("</untrusted>") == 1
    hub.set_policy("deny", "up", "create_*", agent="grok")
    g = store.ensure_agent("grok")
    with pytest.raises(CommonsError):
        hub.call("up__create_note", {"text": "x"}, g)
    assert "create_note" not in hub.search("note", agent_id=g)["text"]
    calls = hub.calls()
    assert {c["status"] for c in calls} >= {"ok", "approval_required", "denied"}
    assert all("tok_live" not in json.dumps(c) for c in calls)


def test_approved_destructive_call_runs_once(hub, store):
    a = store.ensure_agent("cursor")
    p = hub.call("up__delete_everything", {"confirm": True}, a)
    hub.decide(p["approval_id"], True)
    assert "deleted all" in hub.call("up__delete_everything", {"confirm": True}, a, p["approval_id"])["result"]
    assert hub.call("up__delete_everything", {"confirm": True}, a, p["approval_id"])["status"] == "approval_required"


def test_rate_limit_and_health_down(store, monkeypatch):
    h = Hub(store)
    h.add("broken", command="/nonexistent/bin", discover=True)
    assert h.servers()[0]["status"] == "down"
    monkeypatch.setenv("UPSTREAM_TOKEN", "x")
    h.add("up2", command=sys.executable, args=[UP], env_refs={"UPSTREAM_TOKEN": "env://UPSTREAM_TOKEN"}, rate_per_min=2)
    a = store.ensure_agent("x")
    h.call("up2__create_note", {"text": "1"}, a)
    h.call("up2__create_note", {"text": "2"}, a)
    with pytest.raises(CommonsError):
        h.call("up2__create_note", {"text": "3"}, a)


def test_import_configs_and_catalog(store, tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "commons": {"command": "commons", "args": ["serve"]},
        "github": {"command": "docker", "args": ["run", "-i", "ghcr.io/github/github-mcp-server"],
                   "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_literalvalue123"}},
        "linear": {"url": "https://mcp.linear.app/mcp", "headers": {"Authorization": "Bearer lin_api_secret"}}}}))
    r = Hub(store).import_configs([str(cfg)])
    names = {a["name"] for a in r["added"]}
    assert names == {"github", "linear"} and any("commons" in s for s in r["skipped"])
    dump = json.dumps(Hub(store).servers())
    assert "ghp_literal" not in dump and "lin_api_secret" not in dump and "env://GITHUB_PERSONAL_ACCESS_TOKEN" in dump
    cat = load_catalog()["presets"]
    assert cat["github"]["verified"] and cat["zapier"]["verified"] is False
    assert all(p.get("docs") for p in cat.values())
    Hub(store).add_preset("sentry", params={"org": "acme", "project": "web"}, discover=False)
    assert Hub(store).servers()[-1]["url"] == "https://mcp.sentry.dev/mcp/acme/web" or \
        any(s["url"] == "https://mcp.sentry.dev/mcp/acme/web" for s in Hub(store).servers())
    Hub(store).add_preset("stripe", refs={"STRIPE_AGENT_KEY": "env://STRIPE_KEY"}, discover=False)
    st = next(s for s in Hub(store).servers() if s["name"] == "stripe")
    assert st["header_refs"]["Authorization"] == "Bearer {env://STRIPE_KEY}"
    assert classify("delete_repo") == "destructive" and classify("list_issues") == "read" and classify("create_issue") == "write"


def test_gateway_tools_inside_event_loop(store, monkeypatch):
    import asyncio

    from commons_memory.server import build_server
    monkeypatch.setenv("UPSTREAM_TOKEN", "x")
    Hub(store).add("up", command=sys.executable, args=[UP], env_refs={"UPSTREAM_TOKEN": "env://UPSTREAM_TOKEN"})
    mcp = build_server(store)

    async def go():
        s = await mcp.call_tool("mcp_search", {"query": "weather"})
        c = await mcp.call_tool("mcp_call", {"tool": "up__get_weather", "args": {"city": "Rome"}, "agent": "t"})
        return str(s), str(c)
    s, c = asyncio.run(go())
    assert "up__get_weather" in s and "Rome: sunny" in c
