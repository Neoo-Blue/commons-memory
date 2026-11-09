"""v0.38 Canon: one global timeline for product/work/company/vendor facts, owner-only predicates, decisions that set
facts and retire notes, canon()/changes(), FACTS in pickup, changes in turn(), and the write-path fixes it needs."""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from commons_memory import canon as C
from commons_memory.facts import Facts
from commons_memory.orchestrator import Orchestrator
from commons_memory.server import INSTRUCTIONS, build_server
from commons_memory.store import Store

ROOT = Path(__file__).resolve().parents[1]
OLD = ["$9 up to 10 people", "$19 up to 20 people", "$29 up to 40 people"]
NEW = ["$19 up to 10 people", "$39 up to 20 people", "$69 up to 40 people"]


def text(res):
    return (res[0] if isinstance(res, tuple) else res)[0].text


async def tool(mcp, _tool, **args):
    return text(await mcp.call_tool(_tool, args))


async def js(mcp, _tool, **args):
    return json.loads(await tool(mcp, _tool, **args))


@pytest.fixture
def mcp(tmp_path):
    return build_server(Store(str(tmp_path / "c.db"), embedder="hash"))


def test_canon_subjects_share_one_timeline_whatever_the_project(store):
    f = Facts(store)
    a = f.set("product:santa", "this_week", "week one", project="pulse")
    b = f.set("product:santa", "this_week", "week two", project="santawishes")
    assert b["closed"] == [a["id"]] and b["old"] == "week one"
    for p in (None, "pulse", "santawishes", "elsewhere"):
        assert [x["value"] for x in f.get("product:santa", "this_week", project=p)] == ["week two"]
        assert [x["value"] for x in f.history("product:santa", project=p)] == ["week one", "week two"]
    assert not store.q("SELECT 1 FROM items WHERE kind='fact' AND project_id IS NOT NULL")
    # lists are stored in one form, and setting the current value again (no decision behind it) changes nothing
    f.set("product:santa", "features", '["calls", "texts"]')
    again = f.set("product:santa", "features", ["calls", "texts"])
    assert again["unchanged"] and len(f.history("product:santa", "features")) == 1
    assert C.kind_of("company") == "company" and C.kind_of("vendor:sentry") == "vendor" and not C.kind_of("companyx")
    # any other subject keeps its per-project timeline
    f.set("alice", "employer", "Acme", project="a")
    f.set("alice", "employer", "Globex", project="b")
    assert sorted(x["value"] for x in f.get("alice", "employer")) == ["Acme", "Globex"]


def test_migration_moves_legacy_this_week_rows_onto_the_global_timeline(store):
    old = store.add_item("fact", "product:santa this_week w1", project="pulse", subject="product:santa",
                         predicate="this_week", value="w1", check_conflicts=False)["id"]
    time.sleep(0.01)
    new = store.add_item("fact", "product:santa this_week w2", project="pulse-dev", subject="product:santa",
                         predicate="this_week", value="w2", check_conflicts=False)["id"]
    other = store.add_item("fact", "alice employer Acme", project="pulse", subject="alice", predicate="employer",
                           value="Acme", check_conflicts=False)["id"]
    store.x("DELETE FROM meta WHERE key='canon_global'")
    r = C.migrate(store)
    assert r["moved"] == 2 and r["closed"] == [old] and C.migrate(store) == {}  # once
    rows = {x["id"]: x for x in store.q("SELECT id, project_id, superseded_by FROM items WHERE kind='fact'")}
    assert rows[old]["project_id"] is None and rows[new]["project_id"] is None and rows[old]["superseded_by"] == new
    assert rows[other]["project_id"] is not None  # not a Canon subject: untouched
    assert [x["value"] for x in Facts(store).get("product:santa", "this_week")] == ["w2"]
    # Pulse keeps writing this_week from its own project, onto the same timeline
    nxt = Facts(store).set("product:santa", "this_week", "w3", project="pulse", agent_id=store.ensure_agent("pulse"))
    assert nxt["closed"] == [new]


async def test_owner_only_predicates_change_only_by_decision_or_the_pulse_relay(mcp):
    st = mcp.store
    with pytest.raises(Exception, match="owner-only.*record_decision"):
        await tool(mcp, "fact_set", subject="product:santa", predicate="prices", value=json.dumps(NEW), agent="claude",
                   evidence="stripe")
    with pytest.raises(Exception, match="evidence"):  # agent-owned Canon facts need evidence
        await tool(mcp, "fact_set", subject="product:poise", predicate="platforms", value=["Windows", "Mac"],
                   agent="claude")
    ok = await js(mcp, "fact_set", subject="product:poise", predicate="platforms", value=["Windows", "Mac"],
                  agent="claude", evidence="https://github.com/Neoo-Blue/poise/releases/tag/v1.4")
    assert json.loads(st.q("SELECT meta FROM items WHERE id=?", (ok["id"],))[0]["meta"])["evidence"].endswith("v1.4")
    with pytest.raises(Exception, match="owner-only"):
        await tool(mcp, "fact_invalidate", subject="work:cooli", predicate="do_not_merge", agent="claude")
    with pytest.raises(Exception, match="keep their history"):  # not even the agent that wrote it deletes one
        await tool(mcp, "forget", id=ok["id"], agent="claude")
    with pytest.raises(Exception, match="is Canon"):  # remember(kind=fact) would bypass the guard and the timeline
        await tool(mcp, "remember", content="santa prices", kind="fact", subject="product:santa", predicate="prices",
                   value="$9", agent="claude")
    # the Pulse relay carries the owner's Pulse Studio edits
    relayed = await js(mcp, "fact_set", subject="product:santa", predicate="prices", value=OLD, agent="pulse")
    assert relayed["id"].startswith("fct_")
    # company.owner_only overrides the default list (and is itself owner-only: set through a decision)
    await js(mcp, "record_decision", what="Only prices are owner-only", why="owner: 'just prices'", agent="claude",
             sets=[{"subject": "company", "predicate": "owner_only", "value": ["product.prices"]}])
    assert C.owner_only(st) == ["product.prices"]
    await js(mcp, "fact_set", subject="work:cooli", predicate="status", value="paused", agent="claude", evidence="dec")


async def test_record_decision_sets_supersedes_and_returns_stale_notes(mcp):
    st = mcp.store
    await js(mcp, "fact_set", subject="product:secret", predicate="prices", value=OLD, agent="pulse")
    stale = await js(mcp, "remember", content="Secret Santa Hotline: $9 up to 10 people, $19 up to 20 people",
                     project="santawishes", agent="claude")
    fresh = await js(mcp, "remember", content="Hotline pricing is $19 up to 10 people now", agent="claude")
    wrong = await js(mcp, "remember", content="The hotline costs nine dollars", agent="claude")
    with pytest.raises(Exception, match="sets item"):
        await tool(mcp, "record_decision", what="x", why="y", sets=[{"subject": "product:secret"}], agent="claude")
    with pytest.raises(Exception, match="no item"):
        await tool(mcp, "record_decision", what="x", why="y", supersedes=["mem_nope"], agent="claude")
    assert not st.q("SELECT 1 FROM items WHERE kind='decision'")  # nothing written when the input is bad
    d = await js(mcp, "record_decision", what="Hotline prices are $19/$39/$69", why="owner: 'make it 19, 39, 69'",
                 agent="claude", sets=[{"subject": "product:secret", "predicate": "prices", "value": NEW}],
                 supersedes=[wrong["id"]])
    assert d["id"].startswith("dec_") and d["superseded"] == [wrong["id"]]
    assert d["set"][0]["value"] == NEW and d["set"][0]["old"] == OLD
    assert stale["id"] in d["related"] and fresh["id"] not in d["related"] and wrong["id"] not in d["related"]
    assert [x["value"] for x in Facts(st).get("product:secret", "prices")] == [json.dumps(NEW)]
    assert st.q("SELECT superseded_by FROM items WHERE id=?", (wrong["id"],))[0]["superseded_by"] == d["id"]
    meta = json.loads(st.q("SELECT meta FROM items WHERE id=?", (d["id"],))[0]["meta"])
    assert meta["sets"][0]["predicate"] == "prices" and meta["supersedes"] == [wrong["id"]]
    assert d["id"] in [x["id"] for x in st.why(d["set"][0]["id"])["decisions"]]  # the fact knows its decision
    plain = await js(mcp, "record_decision", what="Use SQLite", why="simple", agent="claude")
    assert set(plain) == {"id", "kind", "summary"}  # without sets/supersedes nothing changes


async def test_canon_and_changes_render_the_record(mcp):
    rules = [f"rule {i}" for i in range(30)]
    await js(mcp, "record_decision", what="Seed Canon", why="owner approved the seed", agent="claude", sets=[
        {"subject": "company", "predicate": "standing_rules", "value": rules},
        {"subject": "company", "predicate": "focus", "value": ["santa", "toastline"]},
        {"subject": "product:santa", "predicate": "offer", "value": "A call from Santa"},
        {"subject": "product:santa", "predicate": "prices", "value": OLD},
        {"subject": "work:santa", "predicate": "status", "value": "active"},
        {"subject": "work:santa", "predicate": "repos", "value": ["https://github.com/Neoo-Blue/santawishes"]},
        {"subject": "product:toastline", "predicate": "offer", "value": "Toasts, recorded"},
        {"subject": "vendor:sentry", "predicate": "terms", "value": "grant: 1 year free"}])
    await js(mcp, "fact_set", subject="product:santa", predicate="this_week", value="Pulse note", agent="pulse")
    t0 = int(time.time() * 1000)
    time.sleep(0.01)
    d = await js(mcp, "record_decision", what="New Santa prices", why="owner: 'raise them'", agent="claude",
                 sets=[{"subject": "product:santa", "predicate": "prices", "value": NEW}])
    full = await js(mcp, "canon")
    assert set(full) >= {"core", "products", "card", "changes", "open_conflicts", "generated_at", "vendors"}
    assert full["products"]["santa"]["product"]["prices"] == NEW and full["products"]["santa"]["work"]["status"] == "active"
    assert "this_week" not in full["products"]["santa"]["product"]  # Pulse's weekly note is not part of the record
    assert full["vendors"] == {"sentry": {"terms": "grant: 1 year free"}} and full["open_conflicts"] == 0
    core = full["core"].splitlines()
    assert len(core) <= 20 and C.PRECEDENCE in full["core"] and "product.prices" in full["core"]
    assert "santa [active]: A call from Santa | prices: $19 up to 10 people" in full["card"]  # one-liners
    one = await js(mcp, "canon", repo="git@github.com:Neoo-Blue/santawishes.git")
    assert one["product"] == "santa" and list(one["products"]) == ["santa"]
    assert "- prices: $19 up to 10 people; $39 up to 20 people; $69 up to 40 people" in one["card"]
    assert "- work.status: active" in one["card"]
    assert {c["subject"] for c in one["changes"]} == {"company", "product:santa", "work:santa", "vendor:sentry"}
    assert (await js(mcp, "canon", repo="Neoo-Blue/unknown"))["product"] is None
    ch = await js(mcp, "changes", since=t0)
    assert [(c["subject"], c["predicate"], c["old"], c["new"], c["by"], c["decision"]) for c in ch] == [
        ("product:santa", "prices", OLD, NEW, "claude", d["id"])]
    assert await js(mcp, "changes", since="2999-01-01T00:00:00Z") == []
    assert len(await js(mcp, "changes")) == len(await js(mcp, "changes", since="1d")) == 9  # 8 seeded, 1 changed
    via = json.loads(await tool(mcp, "call", tool="canon", args={"product": "santa"}))
    assert via["product"] == "santa"
    assert json.loads(await tool(mcp, "call", tool="changes", args={"since": t0}))[0]["new"] == NEW


async def test_fact_and_canon_tools_are_in_the_default_profile(mcp, monkeypatch):
    from commons_memory.toolprofile import CORE
    monkeypatch.delenv("COMMONS_TOOLS", raising=False)
    assert {"fact_set", "fact_get", "fact_history", "canon", "changes"} <= set(CORE)
    assert {"fact_set", "fact_get", "fact_history", "canon", "changes"} <= {t.name for t in await mcp.list_tools()}


async def test_pickup_opens_with_facts_never_trimmed_and_turn_adds_changes_once(mcp):
    st = mcp.store
    aid = st.ensure_agent("vivi")
    o = Orchestrator(st)
    assert o.pickup(aid)["facts"] == ""  # no Canon, no conflicts: no section
    sets = [{"subject": f"product:{k}", "predicate": "offer", "value": f"{k} offer"} for k in ("santa", "toastline")]
    sets += [{"subject": "work:santa", "predicate": "repos", "value": ["https://github.com/Neoo-Blue/santawishes"]}]
    await js(mcp, "record_decision", what="Seed", why="owner", agent="claude", sets=sets)
    pk = o.pickup(aid, max_tokens=5)
    assert pk["text"].startswith("FACTS") and "COMPANY CORE" in pk["text"] and C.PRECEDENCE in pk["text"]
    assert "- santa: santa offer" in pk["facts"] and "- toastline: toastline offer" in pk["facts"]  # one-liners
    from commons_memory import work as W
    W.start(st, aid, "fix the hotline prices on the site", "santawishes")
    mine = o.pickup(aid, max_tokens=5)["facts"]
    assert "santa (product:santa" in mine and "- offer: santa offer" in mine and "toastline" not in mine
    out = await tool(mcp, "pickup", agent="vivi", max_tokens=5)
    assert out.startswith("FACTS") and "COMMONS PLAYBOOK" in out
    from commons_memory.capture import Capture  # the SessionStart pack keeps FACTS whole under its header
    pack = Capture(st).context(aid, "santawishes", max_tokens=40)
    assert pack.startswith("# Commons memory (santawishes)\n\nFACTS") and "- offer: santa offer" in pack
    # a conflict on an owner-only fact is counted, refused to agents, and settled by the owner's decision
    await js(mcp, "fact_set", subject="product:santa", predicate="prices", value=NEW, agent="pulse")
    await js(mcp, "remember", content="Santa costs $9", subject="product:santa", predicate="prices", value="$9",
             agent="claude")
    cfl = st.list_conflicts()
    assert len(cfl) == 1 and (await js(mcp, "canon"))["open_conflicts"] == 1
    assert "OPEN CONFLICTS: 1, 1 on owner-only facts" in o.pickup(aid)["facts"]
    with pytest.raises(Exception, match="owner-only fact: agents never resolve it"):
        await tool(mcp, "conflicts", resolve_id=cfl[0]["id"], keep_id=cfl[0]["a"], agent="claude")
    await js(mcp, "record_decision", what="Santa prices stay", why="owner: 'keep them'", agent="claude",
             sets=[{"subject": "product:santa", "predicate": "prices", "value": NEW}])
    assert not st.list_conflicts() and "OPEN CONFLICTS" not in o.pickup(aid)["facts"]
    # turn(): Canon changes since this agent's last turn, each shown once
    first = (await js(mcp, "turn", user_message="what is new with the santa line", agent="muse"))["context"]
    assert "CANON CHANGES since your last turn" in first and "product:santa.offer" in first
    again = (await js(mcp, "turn", user_message="and anything else today", agent="muse"))["context"]
    assert "CANON CHANGES" not in again
    await js(mcp, "fact_set", subject="product:toastline", predicate="features", value=["rooms"], agent="claude",
             evidence="https://toastline.example/changelog")
    later = (await js(mcp, "turn", user_message="anything new for toastline", agent="muse"))["context"]
    assert "product:toastline.features: (none) -> rooms (by claude)" in later and "santa" not in later


def test_librarian_keeps_the_newer_near_duplicate_and_never_merges_canon_facts(store, monkeypatch):
    from commons_memory.librarian import Librarian
    monkeypatch.setenv("COMMONS_LIBRARIAN_AUTO_APPROVE", "1")
    old = store.add_item("memory", "The Hotline costs $9 up to 10 people and $19 up to 20 people", project="s")["id"]
    time.sleep(0.01)
    new = store.add_item("memory", "The Hotline costs $19 up to 10 people and $39 up to 20 people", project="s")["id"]
    places = [f"page {w} of the site" for w in ("alpha beta gamma delta epsilon zeta eta theta iota kappa".split())]
    f = Facts(store)
    a = f.set("work:santa", "places", places)["id"]
    b = f.set("work:toastline", "places", places)["id"]
    rep = Librarian(store).run(full=True)
    assert old in rep["dedupe"]["merged"]
    sup = {r["id"]: r["superseded_by"] for r in store.q("SELECT id, superseded_by FROM items")}
    assert sup[old] == new and sup[new] is None  # the update wins
    assert sup[a] is None and sup[b] is None  # Canon facts keep their own timeline


def test_captured_chat_decisions_are_observations(store):
    from commons_memory.capture import Capture
    aid = store.ensure_agent("claude")
    r = Capture(store).event(aid, "stop", {"project": "shop", "session_id": "s9", "transcript_tail":
                                           "assistant: We decided to use signed cookies instead of server sessions."},
                             "claude-code")
    assert any(i["kind"] == "decision" for i in r["items"])
    assert not store.q("SELECT 1 FROM items WHERE kind='decision'")
    row = store.q("SELECT kind, tags FROM items WHERE content LIKE '%We decided%' AND tags NOT LIKE '%conversation%'")
    assert [(r["kind"], json.loads(r["tags"])) for r in row] == [("observation", ["observation", "decision"])]
    assert not store.q("SELECT 1 FROM items WHERE content LIKE '%captured from session%'")


def test_importer_reimports_edited_files_as_superseding_versions(store):
    from commons_memory.importers import import_items
    item = {"kind": "memory", "title": "Santa prices", "content": "Hotline $9/$19/$29", "src": "claude-memory:x/santa.md"}
    assert import_items(store, [item])["added"] == 1
    assert import_items(store, [item])["skipped"] == 1  # unchanged: skipped
    first = store.q("SELECT value FROM meta WHERE key='import:claude-memory:x/santa.md'")[0]["value"]
    r = import_items(store, [item | {"content": "Hotline $19/$39/$69"}])
    assert r["added"] == 1 and r["updated"] == 1
    second = store.q("SELECT value FROM meta WHERE key='import:claude-memory:x/santa.md'")[0]["value"]
    assert second != first
    assert store.q("SELECT superseded_by FROM items WHERE id=?", (first,))[0]["superseded_by"] == second
    assert import_items(store, [item | {"content": "Hotline $19/$39/$69"}])["skipped"] == 1
    assert import_items(store, [{"content": "dropped", "src": "x:drop", "status": "drop"}])["decided"] == 1
    assert import_items(store, [{"content": "changed", "src": "x:drop"}])["skipped"] == 1  # a drop stays dropped


def test_playbook_states_the_canon_rules_and_prompt_is_current():
    flat = " ".join(INSTRUCTIONS.split())
    for rule in ("BRIEF:", "WRITE FIRST:", "ASK BY ITEM:", "FINISH:", "Owner-only facts", "1-2 ok", "git fetch",
                 "work:<key>.places", "never restate values"):
        assert rule in flat
    assert C.PRECEDENCE in flat  # stated identically everywhere
    assert not re.search("[\u2013\u2014]", INSTRUCTIONS)
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen_prompt.py"), "--check"], capture_output=True)
    assert r.returncode == 0, r.stderr


async def test_consistency_pass_skill_is_served(mcp):
    sk = await js(mcp, "skill", name="consistency-pass", agent="claude")
    assert sk["status"] == "active" and "never resolve it" in sk["content"]
    for step in ("Check the bridges", "agent-owned", "Ask the owner, batched", "without sets", "Route peer findings",
                 "Retired values", "Digest"):
        assert step in sk["content"]


async def test_conflicts_touching_canon_facts_are_never_settled_by_an_agent_picking_a_side(mcp):
    st = mcp.store
    price = (await js(mcp, "fact_set", subject="product:santa", predicate="prices", value=OLD, agent="pulse"))["id"]
    plat = (await js(mcp, "fact_set", subject="product:poise", predicate="platforms", value=["Windows"],
                     agent="claude", evidence="https://example.com/release"))["id"]
    note = (await js(mcp, "remember", content="Santa is $5 and Poise runs on Linux", agent="claude"))["id"]

    def text_conflict(a, b):  # what the librarian's TypeSafe pass opens for free text
        cid = f"cfl_{a[-6:]}{b[-6:]}"
        st.x("INSERT INTO conflicts VALUES(?,?,?,?,?,?,?)", (cid, a, b, "text", "contradicts", "open", time.time()))
        return cid
    on_price, on_plat = text_conflict(price, note), text_conflict(plat, note)
    assert C.open_conflicts(st) == (2, 1)  # the one touching an owner-only Canon fact counts as owner-only
    for keep in (price, note):
        with pytest.raises(Exception, match="owner-only fact: agents never resolve it"):
            await tool(mcp, "conflicts", resolve_id=on_price, keep_id=keep, agent="claude")
    with pytest.raises(Exception, match="is a Canon fact"):  # an agent-owned Canon fact is corrected by fact_set only
        await tool(mcp, "conflicts", resolve_id=on_plat, keep_id=note, agent="claude")
    await js(mcp, "conflicts", resolve_id=on_plat, keep_id=plat, agent="claude")  # Canon wins over a note: fine
    assert st.q("SELECT superseded_by FROM items WHERE id=?", (note,))[0]["superseded_by"] == plat
    d = await js(mcp, "record_decision", what="Santa prices", why="owner: 'these'", agent="claude",
                 sets=[{"subject": "product:santa", "predicate": "prices", "value": NEW}])
    assert d["set"][0]["old"] == OLD and C.open_conflicts(st) == (0, 0)  # the decision settled the one on the old value
