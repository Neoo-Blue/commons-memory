"""Bugs found by the agents' v0.20 full feature test."""
import base64
import os

from commons_memory import vault as V
from commons_memory import work as W
from commons_memory.capture import Capture
from commons_memory.orchestrator import Orchestrator

from conftest import make_store


def test_a_pinned_node_goes_to_its_agent_and_a_failed_try_stays_there(store):
    me, gb, other = (store.ensure_agent(n) for n in ("lead", "grok-build", "spare"))
    o = Orchestrator(store)
    j = o.submit("feature test", agent_id=me, dag=[{"key": "t", "type": "test", "title": "test it", "agent": "grok-build"}])
    sid = j["subtasks"][0]["id"]
    assert store.q("SELECT assigned_agent FROM subtasks WHERE id=?", (sid,))[0]["assigned_agent"] == gb
    assert o.claim(other, sid) is None                      # pinned: nobody else can take it
    assert o.claim(gb, sid)["subtask_id"] == sid
    o.complete(sid, gb, "could not reach the hub", success=False)
    row = store.q("SELECT status, assigned_agent, excluded_agents FROM subtasks WHERE id=?", (sid,))[0]
    assert (row["status"], row["assigned_agent"]) == ("pending", gb) and gb not in (row["excluded_agents"] or "")


def test_vault_names_may_use_hyphens_like_agent_names(store, monkeypatch):
    monkeypatch.setenv("COMMONS_VAULT_KEY", base64.b64encode(os.urandom(32)).decode())
    a = store.ensure_agent("grok-build")
    g = V.generate(store, a, "test-grok-build-login", "example", "https://example.com", purpose="feature test")
    assert any(c["credential"] == "test-grok-build-login" for c in V.listing(store)) and g["password"]


def test_turn_recall_skips_other_agents_asking_the_same_question(tmp_path):
    store = make_store(tmp_path, embedder="fastembed")  # recall's similarity bar needs real embeddings
    store.add_item("memory", "test-antigravity: where does Plex run?", agent_id=store.ensure_agent("a"),
                   session_id="other-chat", tags=["conversation", "user"])
    store.add_item("memory", "Plex runs in Docker in Proxmox LXC CT 100 at 192.168.50.203 with Tautulli")
    ctx = Capture(store).recall("test-grok-build: where does Plex run?", None)
    assert "CT 100" in ctx and "test-antigravity" not in ctx


def test_turn_in_a_project_still_finds_global_knowledge(tmp_path):
    store = make_store(tmp_path, embedder="fastembed")
    store.add_item("memory", "Plex runs in Docker in Proxmox LXC CT 100 at 192.168.50.203 with Tautulli")
    store.add_item("memory", "unrelated note about the build", project="commons-test")
    assert "CT 100" in Capture(store).recall("where does Plex run on the homelab?", "commons-test")


def test_summary_does_not_repeat_a_title_the_content_starts_with(store):
    r = store.add_item("fact", "test fact: the marker is cobalt-harbor-7", title="test fact")
    assert r["summary"] == "test fact: the marker is cobalt-harbor-7"


def test_start_work_says_ok_busy_or_done_in_one_field(store):
    a, b = store.ensure_agent("a"), store.ensure_agent("b")
    assert W.start(store, a, "shared claim for the feature test", "p")["status"] == "ok"
    assert W.start(store, b, "shared claim for the feature test", "p")["status"] == "busy"


def _wakes(store, sid):
    return [store.label(r["recipient"])[1] for r in store.q("SELECT recipient FROM messages WHERE body LIKE ?", (f"%{sid}%",))]


def test_a_delegated_task_wakes_only_the_agent_it_was_given_to(store):
    sender, rec, star = (store.ensure_agent(n) for n in ("sender", "muse", "star"))
    o = Orchestrator(store)
    store.x("UPDATE agents SET profile=? WHERE id=?", ('{"expertise": {"general": 1.0}}', star))  # router's pick
    d = o.delegate(sender, "muse", "check the feature test", reroute=False)
    assert d["assigned_to"] == "muse" and _wakes(store, d["subtask_id"]) == ["muse"]
    d = o.delegate(sender, "muse", "nobody is live, so it falls back to muse")
    assert d["assigned_to"] == "muse" and _wakes(store, d["subtask_id"]) == ["muse"]


def test_revoking_project_access_is_audited(store):
    from commons_memory.auth import ACL
    a = store.ensure_agent("a")
    acl = ACL(store)
    acl.grant("p", "a", "write")
    acl.revoke("p", "a")
    assert store.q("SELECT 1 FROM audit WHERE action='acl.revoke'")


def test_remember_takes_fact_and_the_dashboard_answers_head(store):
    import asyncio
    import threading
    import urllib.request

    from commons_memory.dashboard import make_server
    from commons_memory.server import build_server
    key = store.register_agent("grok")["api_key"]
    asyncio.run(build_server(store).call_tool("remember", {"fact": "Plex runs on LXC 100", "api_key": key}))
    assert store.q("SELECT 1 FROM items WHERE content='Plex runs on LXC 100'")
    srv = make_server(store, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/", method="HEAD")
        with urllib.request.urlopen(req, timeout=8) as r:
            assert r.status == 200 and r.read() == b""
    finally:
        srv.shutdown()
