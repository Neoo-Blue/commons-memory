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
    assert [m[0] for m in r["moved"]] == [old, new] and r["closed"] == [old] and C.migrate(store) == {}  # once
    rows = {x["id"]: x for x in store.q("SELECT id, project_id, superseded_by FROM items WHERE kind='fact'")}
    assert rows[old]["project_id"] is None and rows[new]["project_id"] is None and rows[old]["superseded_by"] == new
    assert rows[other]["project_id"] is not None  # not a Canon subject: untouched
    assert [x["value"] for x in Facts(store).get("product:santa", "this_week")] == ["w2"]
    # Pulse keeps writing this_week from its own project, onto the same timeline
    nxt = Facts(store).set("product:santa", "this_week", "w3", project="pulse", agent_id=store.ensure_agent("pulse"))
    assert nxt["closed"] == [new]


async def test_owner_only_predicates_change_only_by_decision(mcp):
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
    # no name is the owner's channel: the Pulse relay records the owner's Studio edits as decisions
    with pytest.raises(Exception, match="owner-only"):
        await tool(mcp, "fact_set", subject="product:santa", predicate="prices", value=OLD, agent="pulse",
                   evidence="Pulse Studio")
    relayed = await js(mcp, "record_decision", what="Santa prices from Pulse Studio", why="owner's Studio edit",
                       agent="pulse", sets=[{"subject": "product:santa", "predicate": "prices", "value": OLD}])
    assert relayed["set"][0]["id"].startswith("fct_")
    # company.owner_only overrides the default list (and is itself owner-only: set through a decision)
    await js(mcp, "record_decision", what="Only prices are owner-only", why="owner: 'just prices'", agent="claude",
             sets=[{"subject": "company", "predicate": "owner_only", "value": ["product.prices"]}])
    assert C.owner_only(st) == ["product.prices", "company.owner_only", "company.precedence"]
    await js(mcp, "fact_set", subject="work:cooli", predicate="status", value="paused", agent="claude", evidence="dec")
    for pred in ("owner_only", "precedence"):  # the narrowed list never opens itself or the precedence to agents
        with pytest.raises(Exception, match="owner-only"):
            await tool(mcp, "fact_set", subject="company", predicate=pred, value=["everything"], agent="claude",
                       evidence="me")


async def test_record_decision_sets_supersedes_and_returns_stale_notes(mcp):
    st = mcp.store
    await js(mcp, "record_decision", what="Hotline prices", why="owner", agent="pulse",
             sets=[{"subject": "product:secret", "predicate": "prices", "value": OLD}])
    stale = await js(mcp, "remember", content="Secret Santa Hotline: $9 up to 10 people, $19 up to 20 people",
                     project="santawishes", agent="claude")
    fresh = await js(mcp, "remember", content="Hotline pricing is $19 up to 10 people now", agent="claude")
    wrong = await js(mcp, "remember", content="The hotline costs nine dollars", agent="claude")
    decisions = len(st.q("SELECT 1 FROM items WHERE kind='decision'"))
    with pytest.raises(Exception, match="sets item"):
        await tool(mcp, "record_decision", what="x", why="y", sets=[{"subject": "product:secret"}], agent="claude")
    with pytest.raises(Exception, match="no item"):
        await tool(mcp, "record_decision", what="x", why="y", supersedes=["mem_nope"], agent="claude")
    assert len(st.q("SELECT 1 FROM items WHERE kind='decision'")) == decisions  # nothing written when the input is bad
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
    await js(mcp, "record_decision", what="Santa prices", why="owner", agent="pulse",
             sets=[{"subject": "product:santa", "predicate": "prices", "value": NEW}])
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
    price = (await js(mcp, "record_decision", what="Santa prices", why="owner", agent="pulse",
                      sets=[{"subject": "product:santa", "predicate": "prices", "value": OLD}]))["set"][0]["id"]
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


async def test_no_name_is_the_owner_and_names_ignore_case(mcp):
    st = mcp.store
    st.ensure_agent("pulse")
    for name in ("Pulse", "PULSE", " pulse "):  # no second identity in other letters, with or without a key
        with pytest.raises(Exception, match="names ignore case"):
            st.register_agent(name)
    with pytest.raises(Exception, match="names ignore case"):
        await tool(mcp, "register_agent", name="Pulse")
    # Pulse's own weekly note is transient: any agent writes it with no evidence (the live relay sends none)
    week = await js(mcp, "fact_set", subject="product:santa", predicate="this_week", value="a busy week", agent="vivi")
    assert week["id"].startswith("fct_") and "this_week" not in json.dumps(C.canon(st)["products"])
    with pytest.raises(Exception, match="evidence"):  # every other agent-owned Canon fact still needs evidence
        await tool(mcp, "fact_set", subject="product:santa", predicate="features", value=["calls"], agent="vivi")


async def test_canon_predicates_have_one_form_and_values_say_something(mcp):
    st = mcp.store
    for p in ("Prices", "prices ", " PRICES"):  # one field, one spelling: the owner-only check sees through case
        with pytest.raises(Exception, match="owner-only"):
            await tool(mcp, "fact_set", subject="product:santa", predicate=p, value="$1", agent="claude", evidence="x")
    for p in ("prices​", "set-up fee", "9lives"):
        with pytest.raises(Exception, match="lowercase letters"):
            await tool(mcp, "fact_set", subject="product:santa", predicate=p, value="$1", agent="claude", evidence="x")
        with pytest.raises(Exception, match="lowercase letters"):
            await tool(mcp, "record_decision", what="x", why="owner", agent="claude",
                       sets=[{"subject": "product:santa", "predicate": p, "value": "$1"}])
    with pytest.raises(Exception, match="owner-only"):
        await tool(mcp, "fact_invalidate", subject="work:cooli", predicate="Do_Not_Merge ", agent="claude", evidence="x")
    d = await js(mcp, "record_decision", what="Santa prices", why="owner", agent="claude",
                 sets=[{"subject": "product:santa", "predicate": " Prices", "value": NEW}])
    assert d["set"][0]["predicate"] == "prices" and C.canon(st)["products"]["santa"]["product"] == {"prices": NEW}
    # values that say nothing are refused on both paths; an empty list says that nothing is listed
    for v in ("   ", {}, ""):
        with pytest.raises(Exception, match="says something|needs 'value'"):
            await tool(mcp, "fact_set", subject="product:santa", predicate="platforms", value=v, agent="claude",
                       evidence="x")
        with pytest.raises(Exception, match="says something"):
            Facts(st).set("product:santa", "platforms", v, agent_id=st.ensure_agent("claude"))
        with pytest.raises(Exception, match="sets item"):
            await tool(mcp, "record_decision", what="x", why="owner", agent="claude",
                       sets=[{"subject": "product:santa", "predicate": "platforms", "value": v}])
    await js(mcp, "fact_set", subject="product:santa", predicate="platforms", value=[], agent="claude", evidence="x")
    assert C.canon(st)["products"]["santa"]["product"]["platforms"] == []
    # one value in another form (keys reordered, 19.0 for 19, JSON text) is no change on any path
    t0 = time.time()
    first = await js(mcp, "fact_set", subject="product:santa", predicate="trial_days", value={"days": 7, "card": 19.0},
                     agent="claude", evidence="x")
    again = await js(mcp, "fact_set", subject="product:santa", predicate="trial_days", value='{"card": 19, "days": 7.0}',
                     agent="claude", evidence="x")
    assert again["unchanged"] and again["id"] == first["id"]
    await js(mcp, "record_decision", what="Trial days stay", why="owner", agent="claude",
             sets=[{"subject": "product:santa", "predicate": "trial_days", "value": {"card": 19, "days": 7}}])
    assert [c["new"] for c in C.changes(st, t0) if c["predicate"] == "trial_days"] == [{"days": 7, "card": 19}]


async def test_a_decision_never_supersedes_a_canon_fact_and_related_leaves_canon_out(mcp):
    st = mcp.store
    d = await js(mcp, "record_decision", what="Seed", why="owner", agent="claude", sets=[
        {"subject": "product:secret", "predicate": "prices", "value": OLD},
        {"subject": "product:secret", "predicate": "promises", "value": ["$9 up to 10 people, for life"]}])
    promise = d["set"][1]["id"]
    with pytest.raises(Exception, match="correct Canon with sets"):
        await tool(mcp, "record_decision", what="Drop the promise", why="cleanup", agent="claude", supersedes=[promise])
    note = await js(mcp, "remember", content="Hotline: $9 up to 10 people", agent="claude")
    d2 = await js(mcp, "record_decision", what="New prices", why="owner", agent="claude",
                  sets=[{"subject": "product:secret", "predicate": "prices", "value": NEW}])
    assert note["id"] in d2["related"] and promise not in d2["related"]  # a Canon fact is corrected, never superseded
    assert st.q("SELECT superseded_by FROM items WHERE id=?", (promise,))[0]["superseded_by"] is None
    assert "never superseded" in " ".join(INSTRUCTIONS.split())


async def test_canon_facts_are_never_restricted_deleted_filed_or_retired_through_a_conflict(mcp, monkeypatch):
    from commons_memory.dashboard import API
    from commons_memory.librarian import Librarian
    st = mcp.store
    d = await js(mcp, "record_decision", what="Seed", why="owner", agent="claude", sets=[
        {"subject": "product:santa", "predicate": "offer", "value": "A call from Santa"},
        {"subject": "product:poise", "predicate": "platforms", "value": ["Windows"]}])
    offer, plat = d["set"][0]["id"], d["set"][1]["id"]
    with pytest.raises(Exception, match="can't be restricted"):
        await tool(mcp, "restrict", id=offer, agents=["claude"], agent="claude")
    # a restriction left from before cannot fork the timeline: the next write closes the hidden row too
    st.x("INSERT INTO row_acl VALUES(?,?,?,?)", (offer, st.ensure_agent("claude"), "read", time.time()))
    r = await js(mcp, "fact_set", subject="product:santa", predicate="offer", value="Calls and texts from Santa",
                 agent="vivi", evidence="https://santa.example/changelog")
    assert r["closed"] == [offer]
    api = API(st)  # the dashboard (every LAN client is admin on a tokenless one) gets the same guards
    for path, body in (("/api/item/delete", {"id": plat}), ("/api/item/update", {"id": plat, "project": "poise"}),
                       ("/api/item/delete", {"id": d["id"]})):
        with pytest.raises(Exception, match="Canon"):
            api.post(path, body)
    with pytest.raises(Exception, match="set Canon facts"):
        await tool(mcp, "forget", id=d["id"], agent="claude")
    note = await js(mcp, "remember", content="Poise runs on Linux", agent="claude")
    cid = "cfl_canon_test"
    st.x("INSERT INTO conflicts VALUES(?,?,?,?,?,?,?)", (cid, plat, note["id"], "text", "contradicts", "open", time.time()))
    with pytest.raises(Exception, match="is a Canon fact"):
        api.post("/api/conflicts/resolve", {"id": cid, "keep": note["id"]})
    await js(mcp, "record_decision", what="Santa prices", why="owner", agent="claude",
             sets=[{"subject": "product:santa", "predicate": "prices", "value": OLD}])
    await js(mcp, "remember", content="Santa costs $5", subject="product:santa", predicate="prices", value="$5",
             agent="claude")
    owner_cfl = [c for c in st.list_conflicts() if c["predicate"] == "prices"][0]
    for keep in (owner_cfl["a"], owner_cfl["b"]):
        with pytest.raises(Exception, match="owner-only fact"):
            api.post("/api/conflicts/resolve", {"id": owner_cfl["id"], "keep": keep})
    # the librarian files unfiled items in the project their text names, but never a Canon fact
    st.project_id("santa")
    monkeypatch.setenv("COMMONS_LIBRARIAN_AUTO_APPROVE", "1")
    Librarian(st).run(full=True)
    assert st.q("SELECT project_id FROM items WHERE id=?", (r["id"],))[0]["project_id"] is None
    Librarian(st).apply_actions(f"PROJECT {r['id']} santa\nMERGE {note['id']} {plat}")
    row = st.q("SELECT project_id, superseded_by FROM items WHERE id=?", (r["id"],))[0]
    assert row["project_id"] is None and st.q("SELECT superseded_by FROM items WHERE id=?", (plat,))[0][0] is None


async def test_fact_set_settles_the_conflicts_on_the_value_it_corrects(mcp):
    st = mcp.store
    plat = (await js(mcp, "fact_set", subject="product:poise", predicate="platforms", value=["Windows"], agent="claude",
                     evidence="https://example.com/v1"))["id"]
    note = await js(mcp, "remember", content="Poise runs on Windows and Mac", subject="product:poise",
                    predicate="platforms", value="Windows; Mac", agent="claude")
    assert note.get("conflicts_with") == [plat] and C.open_conflicts(st) == (1, 0)
    await js(mcp, "fact_set", subject="product:poise", predicate="platforms", value=["Windows", "Mac"], agent="claude",
             evidence="https://example.com/v2")
    assert C.open_conflicts(st) == (0, 0) and not st.q("SELECT 1 FROM items WHERE conflict=1")


def test_record_decision_lands_whole_or_not_at_all(store, monkeypatch):
    from commons_memory import facts as F
    real, calls = F.Facts.set, []

    def flaky(self, subject, *a, **kw):
        calls.append(subject)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return real(self, subject, *a, **kw)
    monkeypatch.setattr(F.Facts, "set", flaky)
    with pytest.raises(RuntimeError):
        store.record_decision("Two prices", "owner", sets=[{"subject": "product:a", "predicate": "prices", "value": "$1"},
                                                           {"subject": "product:b", "predicate": "prices", "value": "$2"}])
    assert not store.q("SELECT 1 FROM items WHERE kind IN ('decision', 'fact')")  # neither the decision nor the first set
    monkeypatch.setattr(F.Facts, "set", real)
    d = store.record_decision("Two prices", "owner", sets=[{"subject": "product:a", "predicate": "prices", "value": "$1"}])
    assert len(store.q("SELECT 1 FROM embeddings WHERE item_id IN (?,?)", (d["id"], d["set"][0]["id"]))) == 2


async def test_fact_invalidate_on_canon_needs_evidence_is_recorded_and_reported(mcp):
    st = mcp.store
    t0 = time.time() - 1
    r = await js(mcp, "fact_set", subject="product:poise", predicate="platforms", value=["Windows", "Mac"], agent="claude",
                 evidence="https://example.com/v1")
    with pytest.raises(Exception, match="evidence"):
        await tool(mcp, "fact_invalidate", subject="product:poise", predicate="platforms", agent="claude")
    with pytest.raises(Exception, match="not later"):
        await tool(mcp, "fact_invalidate", subject="product:poise", predicate="platforms", at="2999-01-01T00:00:00Z",
                   agent="claude", evidence="x")
    out = await js(mcp, "fact_invalidate", subject="product:poise", predicate="platforms", at="2001-01-01T00:00:00Z",
                   agent="vivi", evidence="https://example.com/eol")
    row = st.q("SELECT valid_from, valid_to, meta FROM items WHERE id=?", (r["id"],))[0]
    assert out["invalidated"] == [r["id"]] and row["valid_to"] == row["valid_from"]  # never closed before it opened
    assert json.loads(row["meta"])["ended"]["evidence"] == "https://example.com/eol"
    assert st.q("SELECT agent_id FROM audit WHERE action='fact.invalidate' AND target=?", (r["id"],))[0][0] == \
        st.ensure_agent("vivi")
    ch = C.changes(st, t0)
    assert [(c["predicate"], c["old"], c["new"], c["by"]) for c in ch][-1] == ("platforms", ["Windows", "Mac"], None, "vivi")
    assert "product:poise.platforms: Windows; Mac -> (ended) (by vivi)" in C.news(st, st.ensure_agent("muse"))
    with pytest.raises(Exception, match="no valid_from in the future"):
        await tool(mcp, "fact_set", subject="product:poise", predicate="platforms", value=["Linux"], agent="claude",
                   evidence="x", valid_from="2999-01-01T00:00:00Z")


def test_changes_skip_backfills_and_news_survives_a_late_commit(store):
    f, aid = Facts(store), store.ensure_agent("muse")
    t0 = time.time() - 1
    f.set("product:santa", "offer", "Calls from Santa", agent_id=aid, evidence="x")
    f.set("product:santa", "offer", "Calls from Santa, 2019", valid_from="2019-01-01T00:00:00Z", agent_id=aid)
    assert [c["new"] for c in C.changes(store, t0)] == ["Calls from Santa"]  # the backfill was never current
    assert "Calls from Santa" in C.news(store, aid)
    # a write that commits after a turn read Canon, stamped before that read, is still shown once
    late = f.set("product:santa", "features", ["calls"], agent_id=aid, evidence="x")
    store.x("UPDATE items SET created_at=created_at-5 WHERE id=?", (late["id"],))
    store.x("UPDATE meta SET value=? WHERE key=?", (json.dumps({"t": time.time() - 3 - C.LAG, "ids": []}), f"canon_seen:{aid}"))
    first = C.news(store, aid)
    assert "product:santa.features" in first and C.news(store, aid) == ""


def test_migration_keeps_owner_only_disagreements_for_the_owner_and_records_every_move(store, caplog):
    a = store.add_item("fact", "product:santa prices $9", project="santawishes", subject="product:santa",
                       predicate="prices", value="$9", check_conflicts=False)["id"]
    time.sleep(0.01)
    b = store.add_item("fact", "product:santa prices $19", project="pulse", subject="product:santa", predicate="prices",
                       value="$19", check_conflicts=False)["id"]
    blank_pred = store.add_item("fact", "company note", project="client", subject="company", check_conflicts=False)["id"]
    undated = store.add_item("fact", "work:santa status live", project="ops", subject="work:santa", predicate="status",
                             value="live", check_conflicts=False)["id"]
    store.x("UPDATE items SET created_at=NULL, valid_from=NULL WHERE id=?", (undated,))
    pids = {i: store.q("SELECT project_id FROM items WHERE id=?", (i,))[0][0] for i in (a, b, undated)}
    store.x("DELETE FROM meta WHERE key='canon_global'")
    with caplog.at_level("WARNING"):
        r = C.migrate(store)
    assert sorted(map(tuple, r["moved"])) == sorted((i, pids[i]) for i in (a, b, undated)) and r["closed"] == [a]
    audit = json.loads(store.q("SELECT detail FROM audit WHERE action='canon.migrate'")[0][0])
    assert sorted(map(tuple, audit["moved"])) == sorted(map(tuple, r["moved"])) and "Canon migration" in caplog.text
    assert store.q("SELECT project_id FROM items WHERE id=?", (blank_pred,))[0][0] is not None  # no predicate: left alone
    cfl = store.list_conflicts()
    assert [(c["a"], c["b"], c["predicate"]) for c in cfl] == [(b, a, "prices")] and r["conflicts"] == [cfl[0]["id"]]
    assert C.open_conflicts(store) == (1, 1)
    assert C.canon(store)["products"]["santa"]["work"] == {"status": "live"}  # an undated row is no crash


def test_canon_reads_skip_rows_with_no_predicate_and_use_the_subject_index(store):
    store.add_item("fact", "company memo", subject="company", check_conflicts=False)
    store.add_item("fact", "company memo", subject="company", predicate="", value="x", check_conflicts=False)
    Facts(store).set("company", "focus", ["santa"])
    assert C.canon(store)["company"] == {"focus": ["santa"]} and "COMPANY CORE" in C.facts_section(store, None)
    plan = " ".join(str(tuple(r)) for r in store.q(f"EXPLAIN QUERY PLAN SELECT id FROM items WHERE {C._canon_rows(store)}"))
    assert "items_fact" in plan and "items_kind" not in plan


def test_pickup_builds_no_facts_when_the_hook_client_has_canon(store, monkeypatch):
    from commons_memory.capture import Capture
    built = []
    monkeypatch.setattr(C, "facts_section", lambda *a, **k: built.append(1) or "FACTS (Canon x)")
    aid = store.ensure_agent("claude")
    assert "FACTS (Canon" not in Capture(store).context(aid, "x", facts=False) and built == []
    assert "FACTS (Canon" in Capture(store).context(aid, "x") and built == [1]


def test_an_import_never_supersedes_what_its_caller_may_not_change(store):
    from commons_memory.auth import PRINCIPAL
    from commons_memory.importers import import_items
    a, b = store.ensure_agent("alice"), store.ensure_agent("bob")
    item = {"kind": "memory", "title": "Plan", "content": "v1", "src": "claude-memory:x/plan.md"}
    tok = PRINCIPAL.set(a)
    try:
        first = import_items(store, [item], agent_id=a)
        iid = store.q("SELECT value FROM meta WHERE key='import:claude-memory:x/plan.md'")[0][0]
        from commons_memory.rowacl import restrict
        restrict(store, a, iid, ["alice"], "write")
    finally:
        PRINCIPAL.reset(tok)
    tok = PRINCIPAL.set(b)
    try:
        r = import_items(store, [item | {"content": "v2 by bob"}], agent_id=b)
    finally:
        PRINCIPAL.reset(tok)
    assert first["added"] == 1 and r["skipped"] == 1 and r["updated"] == 0
    assert store.q("SELECT superseded_by FROM items WHERE id=?", (iid,))[0][0] is None


async def test_code_lookup_runs_its_host_check(mcp, monkeypatch):
    from commons_memory import lsp
    monkeypatch.setattr(lsp, "lookup", lambda *a: {"ok": "looked up"})
    assert "looked up" in await tool(mcp, "code_lookup", path="x.py", line=1)
