import json
import sqlite3
import textwrap

from commons_memory.graphexport import export
from commons_memory.graphlinks import rebuild, subgraph
from commons_memory.kb import KB, SECTIONS
from commons_memory.orchestrator import Orchestrator
from commons_memory.secrets import Broker
from commons_memory.usage import Usage


def rels(store, src=None, dst=None, type_=None):
    sql, args = "SELECT src,dst,type FROM relations WHERE 1=1", []
    for col, v in (("src", src), ("dst", dst), ("type", type_)):
        if v:
            sql += f" AND {col}=?"
            args.append(v)
    return [dict(r) for r in store.q(sql, args)]


def populate(store, tmp_path):
    o = Orchestrator(store)
    a, b = store.ensure_agent("claude"), store.ensure_agent("cursor")
    o.profiles.set(a, expertise={"code": 0.9, "plan": 0.9, "summarize": 0.9})
    o.profiles.set(b, expertise={"review": 0.9, "red_team": 0.9, "test": 0.9, "judge": 0.9})
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "billing.py").write_text(textwrap.dedent('''
        class Invoice:
            """An invoice."""
            def total(self):
                return compute_tax(1)

        def compute_tax(x):
            return x
    '''))
    from commons_memory.codeindex import index_repo
    index_repo(store, str(tmp_path), "shop")
    sym = store.q("SELECT id FROM entities WHERE name='compute_tax'")[0]["id"]
    store.add_item("memory", "The billing service uses Stripe webhooks. See https://stripe.com/docs/webhooks and "
                   "github.com/acme/billing for details. Edit app/billing.py to change tax rules.", project="shop",
                   agent_id=a, entities=["Stripe"])
    store.add_item("memory", "To deploy, run `make deploy` from the repo root after tests pass.", project="shop",
                   agent_id=a, tags=["runbook"])
    d = store.record_decision("Use Stripe for payments", "best API and webhooks", ["Adyen"], a, "shop", related_ids=[sym])
    s = store.session_start("tax rules", a, "shop")
    store.handoff(s["session_id"], summary="half", open_questions=["Which VAT rates apply in the EU?"], agent_id=a)
    job = o.submit("Implement tax rounding", pattern="cross_review", project="shop", agent_id=a)
    for _ in range(6):
        for ag in (a, b):
            p = o.claim(ag)
            if p:
                o.complete(p["subtask_id"], ag, "done\nVERDICT: approve", tokens_in=50, model="gpt-4o")
    rec = o.schedule("nightly", "@daily", "Check invoices", project="shop")
    run = o.run_due(force="nightly")[0]
    pref = o.prefs.observe("Use Decimal for money", project="shop", source="agent:claude")
    tpref = o.prefs.observe("Prefer pytest", task_type="test")
    br = Broker(store)
    br.register("env://STRIPE_KEY", "stripe", project="shop", allowed_agents=["claude"], agent_id=a)
    lease = br.request("env://STRIPE_KEY", a)
    store.add_item("memory", "Gardening: tomatoes need sun and tomatoes need water daily.")
    store.add_item("memory", "Gardening tip: water tomatoes in the morning.")
    store.add_item("memory", "Random thought about chess openings.")
    return dict(o=o, a=a, b=b, sym=sym, d=d, job=job, rec=rec, run=run, pref=pref, tpref=tpref, lease=lease)


def test_everything_is_linked(store, tmp_path):
    x = populate(store, tmp_path)
    s = store
    d = x["d"]["id"]
    assert rels(s, d, x["a"], "decided_by") and rels(s, d, x["sym"], "about")
    assert rels(s, d, s.project_id("shop"), "in_project")
    assert rels(s, x["run"]["job_id"], x["rec"]["id"], "run_of")
    wf = s.q("SELECT id FROM entities WHERE type='workflow' AND name='cross_review'")[0]["id"]
    assert rels(s, x["job"]["job_id"], wf, "uses_pattern")
    subs = {r["key"]: r["id"] for r in s.q("SELECT key,id FROM subtasks WHERE job_id=?", (x["job"]["job_id"],))}
    assert rels(s, subs["review"], subs["code"], "reviews") and rels(s, subs["review"], subs["code"], "approves")
    assert rels(s, subs["code"], wf, "uses_pattern")
    assert rels(s, x["pref"]["id"], s.project_id("shop"), "applies_to") and rels(s, x["pref"]["id"], x["a"], "learned_from")
    tt = s.q("SELECT id FROM entities WHERE type='task_type' AND name='test'")[0]["id"]
    assert rels(s, x["tpref"]["id"], tt, "applies_to")
    usage = rels(s, type_="billed_to")
    assert any(r["dst"] == x["job"]["job_id"] for r in usage) and any(r["dst"] == s.project_id("shop") for r in usage)
    assert rels(s, type_="spent_by")
    assert rels(s, "env://STRIPE_KEY", s.project_id("shop"), "in_project")
    assert rels(s, x["lease"]["lease_id"], "env://STRIPE_KEY", "lease_of")
    assert s.label("env://STRIPE_KEY")[0] == "secret_ref" and s.label(x["rec"]["id"])[0] == "schedule"


def test_rebuild_backfills_old_db(store, tmp_path):
    x = populate(store, tmp_path)
    n = store.q("SELECT COUNT(*) c FROM relations")[0]["c"]
    # simulate a v0.2 database: drop all the new link types
    store.x("DELETE FROM relations WHERE type IN ('run_of','uses_pattern','reviews','approves','applies_to','billed_to',"
            "'spent_by','lease_of','decided_by','about','has_type','executed_by','learned_from')")
    assert store.q("SELECT COUNT(*) c FROM relations")[0]["c"] < n
    r = rebuild(store)
    assert r["added"] > 0 and store.q("SELECT COUNT(*) c FROM relations")[0]["c"] >= n
    assert rebuild(store)["added"] == 0  # idempotent
    assert rels(store, x["run"]["job_id"], x["rec"]["id"], "run_of")


def test_scoped_graph_views(store, tmp_path):
    x = populate(store, tmp_path)
    pg = subgraph(store, "project", "shop")
    types = {n["type"] for n in pg["nodes"].values()}
    assert {"project", "decision", "job", "file"} <= types
    assert not any("tomatoes" in n["name"] for n in pg["nodes"].values())
    jg = subgraph(store, "job", x["job"]["job_id"])
    assert any(e["type"] == "reviews" for e in jg["edges"]) and any(n["type"] == "workflow" for n in jg["nodes"].values())
    wg = subgraph(store, "workflow", "cross_review", depth=1)
    assert x["job"]["job_id"] in wg["nodes"]
    sg = subgraph(store, "schedule", x["rec"]["id"], depth=1)
    assert x["run"]["job_id"] in sg["nodes"]
    assert export(jg, "mermaid").startswith("graph LR")


def test_kb_sections_and_citations(store, tmp_path):
    x = populate(store, tmp_path)
    kb = KB(store)
    kb.build("shop")
    full = kb.get("shop", max_tokens=20000)
    t = full["text"]
    for sec in ("Overview", "Key decisions", "Architecture", "Glossary", "References", "How-tos", "People",
                "Open questions", "Timeline", "Preferences"):
        assert sec in t, sec
    assert "Use Stripe for payments" in t and x["d"]["id"] in t                     # decision cited
    assert "Invoice" in t and "compute_tax" in t                                  # architecture from code graph
    assert "<https://stripe.com/docs/webhooks>" in t and "`acme/billing`" in t and "`app/billing.py`" in t
    assert "make deploy" in t                                                     # how-to
    assert "Which VAT rates apply in the EU?" in t                               # open question (leftover)
    assert "Use Decimal for money" in t and "Prefer pytest" not in kb.get("shop", "preferences")["text"]
    assert "**claude**" in t and "**cursor**" in t                               # people
    assert "Stripe" in kb.get("shop", "glossary")["text"]
    # every bullet in the decisions section carries a citation
    dec = kb.get("shop", "decisions")["text"]
    assert all("[" in ln for ln in dec.splitlines() if ln.startswith("- "))
    assert set(full["citations"]) >= {x["d"]["id"]}
    small = kb.get("shop", max_tokens=120)
    assert small["tokens"] <= 120 and "truncated" in small["text"]


def test_kb_misc_topics_incremental_search_and_digest(store, tmp_path, monkeypatch):
    x = populate(store, tmp_path)
    kb = KB(store)
    kb.build_all()
    names = [b["bucket"] for b in kb.list()]
    assert "misc" in names and any(n.startswith("misc/") and "tomato" in n for n in names)
    assert "chess" in kb.get("misc", max_tokens=5000)["text"]
    # incremental: write -> dirty -> rebuilt on read after debounce
    monkeypatch.setenv("COMMONS_KB_DEBOUNCE", "0")
    store.record_decision("Round half-even", "banking convention", project="shop")
    assert any(b["bucket"] == "shop" and b["dirty"] for b in kb.list())
    assert "Round half-even" in kb.get("shop", "decisions")["text"]
    assert not any(b["bucket"] == "shop" and b["dirty"] for b in kb.list())
    monkeypatch.setenv("COMMONS_KB_DEBOUNCE", "3600")
    store.record_decision("Cache tax tables", "speed", project="shop")
    r = kb.get("shop", "decisions")
    assert r["stale"] and "Cache tax tables" not in r["text"]                    # debounced
    assert "Cache tax tables" in kb.get("shop", "decisions")["text"] or kb.refresh_dirty(force=True)
    assert "Cache tax tables" in kb.get("shop", "decisions")["text"]
    assert "Stripe" in kb.search("stripe payments")["text"]
    dg = kb.digest("shop")
    assert dg.startswith("Project KB digest") and len(dg) // 4 <= 170
    pk = x["o"].pickup(x["a"], "shop")
    assert "Project KB digest" in pk["text"]


def test_kb_llm_hook_and_export(store, tmp_path, monkeypatch):
    populate(store, tmp_path)
    monkeypatch.setenv("COMMONS_KB_SUMMARIZER", "echo LLM-SUMMARY")
    kb = KB(store)
    kb.build("shop")
    ov = kb.get("shop", "overview")["text"]
    assert "LLM-SUMMARY" in ov and "Sources: [" in ov
    monkeypatch.delenv("COMMONS_KB_SUMMARIZER")
    out = kb.export_markdown(str(tmp_path / "vault"))
    v = tmp_path / "vault"
    assert (v / "shop" / "index.md").exists() and (v / "shop" / "decisions.md").exists()
    dec = (v / "shop" / "decisions.md").read_text()
    assert "[[records/dec_" in dec
    rec = next((v / "records").glob("dec_*.md")).read_text()
    assert "## Cited in" in rec and "[[shop/decisions]]" in rec
    assert out["files"] > 10


def test_mcp_kb_and_graph_tools(store, tmp_path):
    import asyncio
    from commons_memory.server import build_server
    populate(store, tmp_path)
    mcp = build_server(store)

    async def go():
        t = lambda r: (r[0] if isinstance(r, tuple) else r)[0].text  # noqa: E731
        assert "Key decisions" in t(await mcp.call_tool("kb_get", {"project": "shop"}))
        assert json.loads(t(await mcp.call_tool("kb_list", {})))
        assert "Stripe" in t(await mcp.call_tool("kb_search", {"query": "stripe"}))
        assert json.loads(t(await mcp.call_tool("kb_refresh", {"project": "shop"})))["buckets"] == ["shop"]
        assert "added" in json.loads(t(await mcp.call_tool("graph_rebuild", {})))
        assert t(await mcp.call_tool("graph_view", {"scope": "workflow", "id": "cross_review"})).startswith("graph LR")
    asyncio.run(go())
