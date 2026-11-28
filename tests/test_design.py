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
