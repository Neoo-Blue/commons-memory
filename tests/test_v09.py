"""v0.9 regressions: ACLs on every read path, re-index keeps memory links, claim races, host-filesystem guard."""
import json
import threading

import pytest

from commons_memory.auth import ACL, PRINCIPAL
from commons_memory.capture import Capture
from commons_memory.facts import Facts
from commons_memory.kb import KB
from commons_memory.store import CommonsError


@pytest.fixture
def private(store):
    """Project 'mna' is private to alice; bob is a member of nothing."""
    a, b = store.ensure_agent("alice"), store.ensure_agent("bob")
    secret = store.add_item("memory", "acquisition target is Initech", project="mna", entities=["Initech"])
    pub = store.add_item("memory", "public roadmap mentions an Initech partnership", related_ids=[secret["id"]])
    Facts(store).set("alice", "employer", "Acme", project="mna")
    ses = store.session_start("close the Initech deal", a, project="mna")
    tsk = store.post_task(a, "alice", "draft the term sheet", project="mna")
    Capture(store).event(a, "prompt", {"project": "mna", "prompt": "what is the Initech offer price"}, "claude-code")
    KB(store).build("mna")
    ACL(store).grant("mna", "alice", "write")
    return {"a": a, "b": b, "secret": secret["id"], "pub": pub["id"], "ses": ses["session_id"], "tsk": tsk["task_id"]}


def as_(aid):
    class _Ctx:
        def __enter__(self):
            self.t = PRINCIPAL.set(aid)

        def __exit__(self, *exc):
            PRINCIPAL.reset(self.t)
    return _Ctx()


def test_every_read_path_honours_project_acls(store, private):
    p = private
    with as_(p["b"]):
        assert "mna/" not in KB(store).search("acquisition Initech")["text"]
        assert "mna" not in [b["bucket"] for b in KB(store).list()]
        with pytest.raises(CommonsError):
            KB(store).get("mna")
        assert all("Initech offer" not in o["text"] for o in Capture(store).stream())
        assert Facts(store).get("alice", "employer") == [] and Facts(store).history("alice") == []
        with pytest.raises(CommonsError):
            Facts(store).set("alice", "employer", "EvilCorp", project="mna")
        Facts(store).set("alice", "employer", "EvilCorp")  # unscoped fact: must not close mna's fact
        for i in (p["secret"], p["ses"], p["tsk"]):
            with pytest.raises(CommonsError):
                store.expand(i)
        nb = store.neighbors(p["pub"])
        assert p["secret"] not in nb["nodes"] and all(p["secret"] not in (e["src"], e["dst"]) for e in nb["edges"])
        with pytest.raises(CommonsError):
            store.neighbors(p["secret"])
        g = store.export_graph()
        assert p["secret"] not in g["nodes"] and p["ses"] not in g["nodes"]
        with pytest.raises(CommonsError):
            store.export_graph("mna")
        assert store.graph_query(name="Initech")["matches"] == []
        with pytest.raises(CommonsError):
            store.get_task(p["tsk"], p["b"])
        assert p["secret"] not in store.why(p["secret"]).get("text", "")
    with as_(p["a"]):
        assert "mna/" in KB(store).search("acquisition Initech")["text"]
        assert store.graph_query(name="Initech")["matches"]
        cur = Facts(store).get("alice", "employer", project="mna")
        assert [f["value"] for f in cur] == ["Acme"]  # bob's unscoped set did not close it
        assert store.expand(p["ses"])["goal"].startswith("close")
        assert store.get_task(p["tsk"], p["a"])["body"] == "draft the term sheet"
    assert not store.q("SELECT 1 FROM projects WHERE name='typo-proj'")
    store.export_graph("typo-proj")
    assert not store.q("SELECT 1 FROM projects WHERE name='typo-proj'")  # reads don't create projects


def test_reindex_keeps_memory_links_and_is_atomic(store, tmp_path):
    from commons_memory.codeindex import index_repo
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pay.py").write_text("def charge(amount):\n    return amount\n")
    index_repo(store, str(repo), "shop")
    fn = store.q("SELECT id FROM entities WHERE type='function' AND name LIKE '%charge'")[0]["id"]
    m = store.add_item("memory", "charge() must stay idempotent", project="shop", related_ids=[fn])
    (repo / "pay.py").write_text("def charge(amount, key=None):\n    return amount\n")
    st = index_repo(store, str(repo), "shop")
    new_fn = store.q("SELECT id FROM entities WHERE type='function' AND name LIKE '%charge'")[0]["id"]
    assert st["relinked"] >= 1
    assert store.q("SELECT 1 FROM relations WHERE src=? AND dst=? AND type='related_to'", (m["id"], new_fn))


def test_inbox_claim_race_has_one_winner(store):
    a, b = store.ensure_agent("w1"), store.ensure_agent("w2")
    store.post_task(None, "*", "anyone: rotate the logs")
    got = []
    ts = [threading.Thread(target=lambda x=x: got.append(store.inbox(x, claim=True))) for x in (a, b)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(len(g) for g in got) == 1


def test_remote_agents_cannot_touch_host_filesystem(store, tmp_path, monkeypatch):
    import asyncio
    from commons_memory.server import build_server
    mcp = build_server(store)
    bob = store.register_agent("remote-bob")["id"]
    target = tmp_path / "pwn.txt"
    with as_(bob):
        for name, args in (("export_graph", {"format": "json", "path": str(target)}), ("index_code", {"path": "/etc"})):
            with pytest.raises(Exception):
                asyncio.run(mcp._tool_manager.call_tool(name, args))
    assert not target.exists()
    monkeypatch.setenv("COMMONS_ADMIN_AGENTS", "remote-bob")
    with as_(bob):
        asyncio.run(mcp._tool_manager.call_tool("export_graph", {"format": "json", "path": str(target)}))
    assert json.loads(target.read_text())["nodes"] is not None
    out = asyncio.run(mcp._tool_manager.call_tool("search_index", {"query": "x", "project": "nope"}))
    assert "no such project" in str(out)


def test_update_delete_keep_indexes_in_sync(store):
    import time
    m = store.add_item("memory", "the deploy key lives in vault", tags=["ops"])["id"]
    store.update_item(m, content="the deploy key lives in 1password", tags=["ops", "security"])
    assert m in [r["id"] for r in store.search("1password", mode="keyword")["results"]]
    assert m not in [r["id"] for r in store.search("vault", mode="keyword")["results"]]
    assert m in [r["id"] for r in store.search("security", mode="keyword")["results"]]  # tags are indexed too
    store.delete_item(m)
    assert store.search("1password")["results"] == [] and not store.q("SELECT 1 FROM embeddings WHERE item_id=?", (m,))
    old = store.add_item("memory", "Alice joined Acme", created_at=time.time() - 400 * 86400)["id"]
    new = store.add_item("memory", "Alice left Acme for Initech")["id"]
    ids = [r["id"] for r in store.search("Alice Acme", as_of=time.time() - 200 * 86400)["results"]]
    assert old in ids and new not in ids  # as_of hides what didn't exist yet
    for i in range(30):
        store.add_item("memory", f"note {i}")
    r = store.search("", limit=5)
    assert r["total"] >= 32 and "more" in r["text"]
