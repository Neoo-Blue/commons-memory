import time

from commons_memory.store import Store


def test_remember_and_hybrid_search(store):
    a = store.ensure_agent("claude")
    m1 = store.add_item("memory", "The billing service uses Stripe webhooks to reconcile invoices",
                        project="shop", agent_id=a, tags=["billing"])
    store.add_item("memory", "Frontend is built with React and Vite", project="shop", agent_id=a, tags=["ui"])
    store.add_item("memory", "Unrelated note about gardening tomatoes", project="home", agent_id=a)
    res = store.search("stripe invoices")
    assert res["results"][0]["id"] == m1["id"]
    assert m1["id"] in res["text"]
    # filters
    assert all(r["project"] == "shop" for r in store.search("", project="shop")["results"])
    assert [r["id"] for r in store.search("", tags=["billing"])["results"]] == [m1["id"]]
    assert store.search("", project="nope")["results"] == []
    assert len(store.search("", agent="claude")["results"]) == 3


def test_semantic_mode_fuzzy_match(store):
    m = store.add_item("memory", "authentication middleware validates JWT sessions", project="p")
    res = store.search("authenticate session middleware", mode="semantic")
    assert res["results"] and res["results"][0]["id"] == m["id"]


def test_keyword_only_without_embedder(tmp_path):
    s = Store(str(tmp_path / "n.db"), embedder="none")
    m = s.add_item("memory", "postgres connection pool size is 20")
    assert s.search("connection pool", mode="hybrid")["results"][0]["id"] == m["id"]
    assert s.stats()["embedder"] == "none"


def test_token_budget_and_expand(store):
    for i in range(40):
        store.add_item("memory", f"note number {i} about the deployment pipeline " + "x" * 200, project="p")
    res = store.search("deployment pipeline", max_tokens=150, limit=40)
    assert res["tokens"] <= 150 or len(res["results"]) == 1
    assert len(res["results"]) < 40 and "more" in res["text"]
    full = store.expand(res["results"][0]["id"])
    assert "x" * 200 in full["content"]
    trunc = store.expand(res["results"][0]["id"], max_tokens=10)
    assert "truncated" in trunc["content"]


def test_time_filters_and_timeline(store):
    old = store.add_item("memory", "first event", project="p")
    store.x("UPDATE items SET created_at=? WHERE id=?", (time.time() - 10 * 86400, old["id"]))
    new = store.add_item("memory", "second event", project="p")
    assert [r["id"] for r in store.search("", since="2d")["results"]] == [new["id"]]
    assert [r["id"] for r in store.search("", until="5d")["results"]] == [old["id"]]
    tl = store.timeline(project="p")
    assert [r["id"] for r in tl["results"]] == [old["id"], new["id"]]


def test_entity_filter_links(store):
    m = store.add_item("memory", "cache invalidation bug in user service", entities=["UserService"], project="p")
    store.add_item("memory", "other thing", project="p")
    res = store.search("", entity="UserService")
    assert [r["id"] for r in res["results"]] == [m["id"]]


def test_ids_are_prefixed_and_unique(store):
    ids = {store.add_item("memory", f"n{i}")["id"] for i in range(50)}
    assert len(ids) == 50 and all(i.startswith("mem_") for i in ids)
