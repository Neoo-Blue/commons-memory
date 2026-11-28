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
