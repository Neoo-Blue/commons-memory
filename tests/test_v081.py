import json

from conftest import make_store

from commons_memory.diagrams import code_diagram, job_diagram
from commons_memory.facts import Facts
from commons_memory.spaces import Spaces, audit
from commons_memory.sweep import sweep


def test_temporal_facts(tmp_path):
    s = make_store(tmp_path)
    f = Facts(s)
    a = f.set("alice", "employer", "Acme", valid_from="2022-01-01T00:00:00Z")
    b = f.set("alice", "employer", "Globex", valid_from="2024-03-01T00:00:00Z")
    assert b["closed"] == [a["id"]]
    assert [x["value"] for x in f.get("alice", "employer")] == ["Globex"]
    assert [x["value"] for x in f.get("alice", "employer", as_of="2023-06-01T00:00:00Z")] == ["Acme"]
    assert f.get("alice", "employer", as_of="2021-01-01T00:00:00Z") == []
    # backfill an older value: slots in before Acme, does not supersede the current one
    c = f.set("alice", "employer", "Initech", valid_from="2020-01-01T00:00:00Z")
    assert c["valid_to"].startswith("2022-01-01")
    assert [x["value"] for x in f.get("alice", "employer", as_of="2020-06-01T00:00:00Z")] == ["Initech"]
    assert [x["value"] for x in f.history("alice", "employer")] == ["Initech", "Acme", "Globex"]
    assert not s.list_conflicts()  # temporal succession is not a contradiction
    f.invalidate("alice", "employer", at="2025-01-01T00:00:00Z")
    assert f.get("alice", "employer") == []
    assert [x["value"] for x in f.get("alice", "employer", as_of="2024-12-01T00:00:00Z")] == ["Globex"]
    # relations with validity
    x = s.add_item("memory", "x")["id"]
    y = s.add_item("memory", "y")["id"]
    s.relate(x, y, "depends_on")
    assert f.relations_as_of(x)
    f.end_relation(x, y, "depends_on", at="2000-01-01T00:00:00Z")
    assert not f.relations_as_of(x)


def test_sweep_verbatim_idempotent(tmp_path):
    s = make_store(tmp_path)
    d = tmp_path / "logs"
    d.mkdir()
    (d / "chat.md").write_text("User: we deploy with fly.io on fridays only\n\nAssistant: noted, fly.io deploys on fridays\n")
    r = sweep(s, [str(d / "chat.md")], project="ops")
    assert r["added"] == 2 and r["duplicates"] == 0
    assert sweep(s, [str(d / "chat.md")], project="ops")["added"] == 0
    hits = s.search("when do we deploy", project="ops")
    assert any("fridays" in h["summary"] for h in hits["results"])
    assert sweep(s, [str(d / "chat.md")], dry_run=True)["added"] == 0


def test_spaces_and_audit(tmp_path):
    s = make_store(tmp_path)
    s.add_item("memory", "Bob prefers async standups", project="team", entities=["person:Bob"], tags=["process"])
    s.add_item("memory", "Carol owns billing", project="team", entities=["person:Carol"])
    s.add_item("memory", "dup", project="team")
    s.add_item("memory", "dup", project="team")
    sp = Spaces(s)
    names = {(x["type"], x["name"]) for x in sp.list()}
    assert ("person", "Bob") in names and ("project", "team") in names and ("topic", "process") in names
    r = sp.search("person:bob", "standups")
    assert r["results"] and "Bob" in r["results"][0]["summary"]
    assert sp.search("topic:process", "standups")["results"]
    a = audit(s, "team")
    assert a["items"] == 4 and a["score"] < 100 and any(i["issue"] == "exact duplicates" for i in a["issues"])


def test_diagrams(tmp_path):
    s = make_store(tmp_path)
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text("from pkg import b\n\ndef run():\n    return b.helper()\n")
    (repo / "pkg" / "b.py").write_text("def helper():\n    return 1\n")
    (repo / "pkg" / "__init__.py").write_text("")
    from commons_memory.codeindex import index_repo
    index_repo(s, str(repo), project="demo")
    h = code_diagram(s, "demo")
    data = json.loads(h.split("const G=", 1)[1].split(";document.getElementById('legend')", 1)[0])
    labels = {n["label"] for n in data["nodes"]}
    assert {"a.py", "b.py"} <= labels and data["edges"]
    assert "Archify" in h and "http://" not in h.replace("http://www.w3.org/2000/svg", "")  # self-contained
    from commons_memory.orchestrator import Orchestrator
    o = Orchestrator(s)
    j = o.submit("Build thing", project="demo")
    jid = j.get("job_id") or j.get("id")
    hj = job_diagram(s, jid)
    assert "Job plan: Build thing" in hj and "dep" in hj


def test_three_step_search_tools(tmp_path):
    import asyncio

    from commons_memory.server import build_server
    s = make_store(tmp_path)
    ids = [s.add_item("memory", f"step {i}: migrate billing database to postgres", project="p")["id"] for i in range(4)]
    m = build_server(s)

    def call(n, a):
        r = asyncio.run(m._tool_manager.call_tool(n, a))
        return r if isinstance(r, str) else str(r)
    idx = call("search_index", {"query": "billing postgres", "project": "p"})
    assert ids[0] in idx and "timeline_around" in idx
    tl = call("timeline_around", {"anchor": ids[1]})
    assert f"> {ids[1]}" in tl and ids[0] in tl and ids[2] in tl
    got = call("get_items", {"ids": ids[:2]})
    assert ids[0] in got and ids[1] in got
