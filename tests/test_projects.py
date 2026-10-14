"""Projects: raw names grouped into one product, tasks vs projects, folders, the timeline and recorded changes."""
from commons_memory import projects as P
from commons_memory.dashboard import API
from commons_memory.ids import now
from commons_memory.orchestrator import Orchestrator
from conftest import make_store

DAY = 86400


def _seed(s):
    a, b = s.ensure_agent("claude-code"), s.ensure_agent("grok")
    t = now() - 5 * DAY
    for d in range(3):
        s.add_item("memory", f"commons search note {d}", project="commons-memory", agent_id=a, created_at=t + d * DAY)
    s.add_item("decision", "Ship the dashboard as five workspaces", project="commons-memory", agent_id=a, created_at=t + DAY)
    s.add_item("handoff", "HANDOFF audit chain merged, prune anchors", project="commons-memory", agent_id=b, created_at=t + 2 * DAY)
    s.session_for(b, "commons-dev", "benchmark the neighbour rule")
    s.session_for(a, "commons-dev", "write the projects page")
    s.add_item("memory", "live hub restarted", project="commons-live", agent_id=a)
    for i in range(3):
        s.add_item("memory", f"shell history {i}", project="home", agent_id=a)
    s.add_item("memory", "fixed a typo in the newsletter", project="quick-fix", agent_id=b)
    o = Orchestrator(s)
    j = o.submit("Add a projects page", pattern="parallel", project="commons-dev", title="Add a projects page",
                 dag=[{"key": "ui", "type": "code", "title": "Build the page", "body": "", "deps": []}])["job_id"]
    s.x("UPDATE subtasks SET status='done', claimed_by=?, result='TESTS: 12 passed', updated_at=? WHERE job_id=?", (b, now(), j))
    loose = o.submit("Rename a file", pattern="parallel", title="Rename a file",
                     dag=[{"key": "x", "type": "general", "title": "rename", "body": "", "deps": []}])["job_id"]
    return a, b, j, loose


def test_listing_sorts_projects_tasks_folders_and_suggests_groups(tmp_path):
    s = make_store(tmp_path, "pl.db")
    _, _, _, loose = _seed(s)
    out = P.listing(s)
    names = {e["name"]: e["kind"] for e in out["projects"] + out["tasks"] + out["folders"]}
    assert names["commons-memory"] == "project" and names["commons-dev"] == "project"     # 3 days / 2 sessions + a job
    assert names["quick-fix"] == "task" and names["home"] == "folder" and names["commons-live"] == "task"
    assert loose in [j["id"] for j in out["jobs"]]
    g = [x for x in out["suggestions"] if x["kind"] == "group"]
    assert len(g) == 1 and g[0]["title"] == "Commons" and g[0]["members"][0]["name"] == "commons-memory"
    assert {m["name"] for m in g[0]["members"]} == {"commons-memory", "commons-dev", "commons-live"}


def test_grouping_folds_names_into_one_project_with_summed_history(tmp_path):
    s = make_store(tmp_path, "pg.db")
    a, b, job, _ = _seed(s)
    sug = P.listing(s)["suggestions"][0]
    d = P.group(s, sug["into"], [m["id"] for m in sug["members"]], "Commons")
    assert d["title"] == "Commons" and {m["name"] for m in d["members"]} == {"commons-memory", "commons-dev", "commons-live"}
    assert d["counts"]["sessions"] == 2 and d["counts"]["jobs"] == 1 and d["counts"]["decisions"] == 1
    assert {p["agent"] for p in d["people"]} == {"claude-code", "grok"}
    assert d["jobs"][0]["id"] == job and d["jobs"][0]["done"] == 1
    assert d["decisions"][0]["title"].startswith("Ship the dashboard") and d["handoffs"]
    out = P.listing(s)
    assert [e["title"] for e in out["projects"]] == ["Commons"] and out["suggestions"] == []
    assert P.detail(s, "commons-dev")["id"] == d["id"]                    # any member name opens the project
    P.ungroup(s, [m["id"] for m in d["members"] if m["name"] == "commons-dev"][0])
    assert len(P.listing(s)["projects"]) == 2


def test_timeline_merges_everything_newest_first_and_pages_without_overlap(tmp_path):
    s = make_store(tmp_path, "pt.db")
    _seed(s)
    sug = P.listing(s)["suggestions"][0]
    pid = P.group(s, sug["into"], [m["id"] for m in sug["members"]])["id"]
    hi = P.timeline(s, pid, limit=200)["events"]
    kinds = {e["type"] for e in hi}
    assert {"decision", "handoff", "session", "job", "step"} <= kinds and "memory" not in kinds
    assert [e["at"] for e in hi] == sorted((e["at"] for e in hi), reverse=True)
    assert "memory" in {e["type"] for e in P.timeline(s, pid, types="all", limit=200)["events"]}
    first = P.timeline(s, pid, types="all", limit=3)
    rest = P.timeline(s, pid, types="all", limit=200, before=first["next"])["events"]
    assert first["next"] and not {(e["at"], e["title"]) for e in first["events"]} & {(e["at"], e["title"]) for e in rest}
    only = P.timeline(s, pid, types="all", limit=200, agent="grok")["events"]
    assert only and all(e["agent"] in ("grok", None) for e in only)


def test_recorded_changes_show_on_the_project_and_open_with_their_diff(tmp_path, monkeypatch):
    from commons_memory.workspace import record_changes
    s = make_store(tmp_path, "pc.db")
    a, b, job, _ = _seed(s)
    st = dict(s.q("SELECT * FROM subtasks WHERE job_id=?", (job,))[0])
    record_changes(s, b, subtask=st, repo="", branch="v0.9", commit="", diff="--- a/x.py\n+++ b/x.py\n@@\n-old\n+new\n",
                   stat=" x.py | 2 +-")
    d = P.detail(s, "commons-dev")
    assert d["counts"]["changes"] == 1 and "+new" in d["changes"][0]["diff"]
    ev = [e for e in P.timeline(s, d["id"], limit=200)["events"] if e["type"] == "change"]
    assert ev and ev[0]["ref"]["kind"] == "change"
    got = API(s).project_change({"path": ev[0]["ref"]["path"]})
    assert "+new" in got["diff"]


def test_overrides_and_dismiss(tmp_path):
    s = make_store(tmp_path, "po.db")
    _seed(s)
    qf = [e for e in P.listing(s)["tasks"] if e["name"] == "quick-fix"][0]
    P.update(s, qf["id"], kind="project", title="Newsletter")
    assert "Newsletter" in [e["title"] for e in P.listing(s)["projects"]]
    P.update(s, qf["id"], kind="folder")
    assert "quick-fix" in [e["name"] for e in P.listing(s)["folders"]]
    key = P.listing(s)["suggestions"][0]["key"]
    P.dismiss(s, key)
    assert P.listing(s)["suggestions"] == []
