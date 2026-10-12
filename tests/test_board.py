import pytest

from commons_memory import board as B
from commons_memory.capture import Capture
from commons_memory.orchestrator import Orchestrator
from commons_memory.store import CommonsError


def test_board_rules_keep_agents_honest(store):
    s = store
    ask = B.add(s, "ask", "Make search faster than mem0 and show the benchmark in the README", project="commons",
                path="retrieval")
    tgt = B.add(s, "target", "search latency", project="commons", path="retrieval", parent=ask["id"],
                metric="search p50", goal="< 50 ms", current="68 ms")
    act = B.add(s, "action", "batch the hit hydration query", project="commons", path="retrieval", parent=ask["id"])
    assert ask["source"] == "user" and ask["status"] == "open"
    with pytest.raises(CommonsError):  # the user's words are never edited
        B.update(s, ask["id"], text="something else")
    with pytest.raises(CommonsError):  # no evidence, no done
        B.update(s, act["id"], status="done")
    with pytest.raises(CommonsError):  # an ask can't be closed while work under it is open
        B.update(s, ask["id"], status="done", evidence="README updated")
    with pytest.raises(CommonsError):  # dropping needs a reason
        B.update(s, tgt["id"], status="dropped")
    B.update(s, act["id"], status="done", evidence="commit abc123")
    B.update(s, tgt["id"], current="24 ms", status="done", evidence="bench scale p50 24.5 ms")
    again = B.add(s, "ask", "make the search faster than mem0 and show the benchmark in the readme", project="commons")
    assert again["id"] == ask["id"] and again["repeated"] and again["repeats"] == 1
    v = B.view(s, "commons")
    assert ask["id"] in v["ids"] and "(asked 2x)" in v["text"] and act["id"] not in v["ids"]
    assert "## commons / retrieval" in v["text"]
    B.update(s, ask["id"], status="done", evidence="README benchmarks section, commit def456")
    assert B.view(s, "commons")["ids"] == []
    assert act["id"] in B.view(s, "commons", status="all")["ids"]


def test_board_is_first_in_pickup_and_repeats_are_flagged_on_prompt(store):
    s = store
    aid = s.ensure_agent("worker")
    ask = B.add(s, "ask", "Add a dark mode toggle to the dashboard header", project="ui")
    pk = Orchestrator(s).pickup(aid, "ui")
    assert pk["text"].startswith("BOARD") and ask["id"] in pk["board"]
    hint = Capture(s).recall("please add a dark mode toggle to the dashboard header", "ui")
    assert hint.startswith("ALREADY ASKED") and ask["id"] in hint
    assert B.get(s, ask["id"])["repeats"] == 1
