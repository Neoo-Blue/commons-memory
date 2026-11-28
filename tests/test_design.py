from commons_memory.capture import Capture
from commons_memory.design import Design, is_design_feedback
from commons_memory.orchestrator import Orchestrator


def test_design_feedback_detection():
    assert is_design_feedback("both sites looks too generic, make them stylish")
    assert is_design_feedback("Toastline the font is beautiful, but too thin to read")
    assert not is_design_feedback("deploy the worker and run the migration")
    assert not is_design_feedback("add a design doc to the repo")  # mentions design, no reaction


def test_rules_profile_capture_and_pickup(store):
    o = Orchestrator(store)
    a = store.ensure_agent("cursor")
    d = Design(store, o.prefs)
    assert "No design ideology yet" in d.profile()["text"] and d.pointer() == ""

    d.feedback("No cartoonish illustration on product sites", section="hard_no", evidence="2026-10-28 owner")
    d.feedback("Plain sans 16px, answer first", section="strong", surface="dashboard")
    d.feedback("Hero type heavy", section="liked", surface="marketing")
    again = d.feedback("no cartoonish illustration on product sites", section="hard_no")
    assert again["reinforced"]

    dash = d.profile("dashboard")["text"]
    assert dash.index("cartoonish") < dash.index("Plain sans")  # hard nos first
    assert "Hero type" not in dash  # another surface's rule is left out

    Capture(store).event(a, "prompt", {"session_id": "s1", "prompt": "this looks so complex, and literally unreadable"},
                         harness="mcp")
    Capture(store).event(a, "prompt", {"session_id": "s1", "prompt": "run the tests"}, harness="mcp")
    fb = d.recent_feedback()
    assert [f["text"] for f in fb] == ["this looks so complex, and literally unreadable"]

    v = d.profile_set("# Ideology\nElegant, themed, moving.", agent_id=a)
    assert v["version"] == 1 and d.profile_set("# v2", agent_id=a)["version"] == 2
    t = d.profile()["text"]
    assert "DESIGN IDEOLOGY v2" in t and "unreadable" not in t  # feedback older than the profile is folded in

    assert "design_profile" in o.pickup(a)["text"]
    ev = d.evidence()["text"]
    assert "unreadable" in ev and "cartoonish" in ev
    assert d.rebuild()["needs_agent"]  # no distiller model configured


def test_owner_adds_rules_and_edits_the_profile_on_the_page(tmp_path):
    import json
    import threading
    import urllib.request

    from commons_memory.dashboard import PAGE, make_server
    from conftest import make_store

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                     headers={"X-Commons": "1", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())

    store = make_store(tmp_path, "design.db")
    srv = make_server(store, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        r = post("/api/design/add", {"text": "Headlines heavier", "section": "liked", "surface": "marketing"})
        assert r["confidence"] == 1.0 and r["section"] == "liked"
        assert post("/api/design/profile", {"body": "# Mine\nElegant."})["version"] == 1
        with urllib.request.urlopen(base + "/api/design", timeout=8) as g:
            d = json.loads(g.read())
        assert d["profile"]["body"].startswith("# Mine")
        assert [x["statement"] for x in d["rules"]] == ["Headlines heavier"]
        assert "design/add" in PAGE and "design/profile" in PAGE
    finally:
        srv.shutdown()


def test_librarian_auto_mode_settles_the_queue_without_the_owner(store):
    from commons_memory.librarian import Librarian
    lib = Librarian(store)
    lib.configure(auto_approve=True)
    a = store.add_item("memory", "the hub runs on port 8770", project="p")
    b = store.add_item("memory", "the hub runs on port 9000", project="p")
    store.x("INSERT INTO conflicts VALUES(?,?,?,?,?,?,?)", ("cfl_t1", a["id"], b["id"], "text", "contradicts", "open", 1.0))
    d1 = store.add_item("memory", "dup one", project="p")
    d2 = store.add_item("memory", "dup one again", project="p")
    store.x("INSERT INTO librarian_proposals VALUES(?,?,?,?,?,?,?,?)",
            ("lbp_m", "merge", d1["id"], '{"keep": "%s", "dup": "%s"}' % (d2["id"], d1["id"]), "dup", "proposed", 1.0, None))
    store.x("INSERT INTO librarian_proposals VALUES(?,?,?,?,?,?,?,?)",
            ("lbp_s", "flag_stale", a["id"], "{}", "old", "proposed", 1.0, None))
    got = lib.settle()
    assert got["resolved"] == 1 and got["merged"] == 1 and got["dismissed"] >= 1
    assert store.get_item(a["id"])["superseded_by"] == b["id"]  # the newer note wins
    assert store.get_item(d1["id"])["superseded_by"] == d2["id"]
    assert lib.proposals() == [] and store.list_conflicts() == []
    assert lib._propose("flag_stale", d2["id"], {}, "old") is None  # auto mode queues no stale flags


def test_librarian_auto_mode_reviews_pending_skills(store):
    from commons_memory.librarian import Librarian
    from commons_memory.skillpool import SkillPool
    sp = SkillPool(store)
    a = store.ensure_agent("ada")
    sp.publish("tidy", "---\nname: tidy\n---\nSort the files by date.", agent_id=a)
    sp.update("tidy", "---\nname: tidy\n---\nSort the files by date, newest first.", agent_id=a)
    vs = store.q("SELECT id, version, status FROM skill_versions ORDER BY version")
    assert [v["status"] for v in vs] == ["pending", "pending"]
    lib = Librarian(store)
    lib.configure(auto_approve=True)
    for v in vs:
        lib._propose("skill_review", v["id"], {}, "pending")
    got = lib.settle()
    assert got["skills_approved"] == 1 and got["skills_rejected"] == 1
    assert [r["status"] for r in store.q("SELECT status FROM skill_versions ORDER BY version")] == ["rejected", "active"]
