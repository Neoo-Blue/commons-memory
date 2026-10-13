"""v0.22: the job workspace keeps every part's result and code changes in one place, reviewers see the diff they
check, the hooks capture each session's git changes, and the dashboard shows it all on one timeline."""
import json
import subprocess

from commons_memory import settings
from commons_memory import workspace as W
from commons_memory import work as WK
from commons_memory.breakdown import breakdown
from commons_memory.capture import Capture, git_changes
from commons_memory.drive import Drive
from commons_memory.orchestrator import Orchestrator

DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
-print("old")
+print("new")
+TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
"""


def _job(store):
    a, r = store.ensure_agent("coder"), store.ensure_agent("reviewer")
    o = Orchestrator(store)
    j = o.submit("fix it", dag=[{"key": "code", "type": "code", "title": "fix", "assign": "coder", "gates": ["rev"]},
                                {"key": "rev", "type": "review", "title": "review it", "assign": "reviewer"}])
    return o, a, r, {x["key"]: x["id"] for x in j["subtasks"]}, j["job_id"]


def test_a_part_lands_in_the_job_workspace_and_its_reviewer_sees_the_diff(store):
    o, a, r, ids, jid = _job(store)
    o.claim(a, ids["code"])
    out = o.complete(ids["code"], a, "fixed the print", changes={"repo": "acme/app", "branch": "main",
                                                                 "commit": "abc1234", "diff": DIFF})
    assert out["workspace"]["files"] == ["app.py"] and out["workspace"]["verified"].startswith("unverified")
    d = Drive(store)
    assert "fixed the print" in d.read(f"/jobs/{jid}/code/result.md")["text"]
    saved = d.read(f"/jobs/{jid}/code/changes.diff")["text"]
    assert '+print("new")' in saved and "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in saved  # secrets redacted
    pk = o.claim(r, ids["rev"])
    seen = pk["workspace"]["changes"][0]
    assert seen["repo"] == "acme/app" and '+print("new")' in seen["diff"]
    assert {f["path"].rsplit("/", 1)[1] for f in pk["workspace"]["files"]} >= {"result.md", "changes.diff", "changes.json"}


def test_finish_work_keeps_the_changes_with_the_claim(store):
    a = store.ensure_agent("solo")
    c = WK.start(store, a, "rename the settings module", "p")["claim_id"]
    out = WK.finish(store, a, c, "renamed", changes={"diff": DIFF})
    assert out["changes"]["folder"] == f"/work/{c}" and W.changes(store, c)["files"] == ["app.py"]


def test_the_hook_files_a_sessions_git_changes_with_the_work_in_hand(store):
    o, a, r, ids, jid = _job(store)
    cap = Capture(store)
    git = {"repo": "acme/app", "branch": "main", "diff": DIFF, "stat": " app.py | 3 ++-"}
    cap.event(a, "stop", {"session_id": "s1", "git": git})  # no claim yet: kept with the session
    assert W.changes(store, "generic:s1")["files"] == ["app.py"]
    o.claim(a, ids["code"])
    cap.event(a, "stop", {"session_id": "s1", "git": git})
    assert W.changes(store, ids["code"])["source"] == "hook"
    settings.put(store, "capture_diffs", False)
    assert not cap.event(r, "stop", {"session_id": "s2", "git": git}).get("changes")  # switched off on Setup


def test_git_changes_sees_commits_made_during_the_session_and_uncommitted_work(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONS_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    run("add", ".")
    run("commit", "-qm", "start")
    assert git_changes(str(repo), "sess", start=True) is None
    assert git_changes(str(repo), "sess") is None  # nothing changed yet
    (repo / "a.txt").write_text("two\n")
    run("commit", "-qam", "during the session")
    (repo / "a.txt").write_text("three\n")
    (repo / "new.txt").write_text("x\n")
    g = git_changes(str(repo), "sess")
    assert g["commit"] and g["commit"] != g["base"] and "+three" in g["diff"] and "-one" in g["diff"]
    assert g["untracked"] == ["new.txt"] and "a.txt" in g["stat"]
    monkeypatch.setenv("COMMONS_HOOK_GIT", "0")
    assert git_changes(str(repo), "sess") is None


def test_the_job_page_shows_one_timeline_with_changes_files_and_an_agent_filter(store):
    o, a, r, ids, jid = _job(store)
    o.claim(a, ids["code"])
    o.complete(ids["code"], a, "fixed", changes={"diff": DIFF})
    b = breakdown(store, jid)
    assert any(e["event"] == "changed code" and e["by"] == "coder" for e in b["timeline"])
    assert b["workspace"]["folder"] == f"/jobs/{jid}" and len(b["workspace"]["files"]) == 3
    assert next(n for n in b["nodes"] if n["key"] == "code")["changes"]["files"] == ["app.py"]
    assert "coder" in b["agents"]
    assert [c["by"] for c in W.recent(store, "coder")] == ["coder"] and W.recent(store, "reviewer") == []
    assert json.loads(Drive(store).read(f"/jobs/{jid}/code/changes.json")["text"])["ref"] == ids["code"]


def test_a_hostile_part_key_stays_inside_the_job_folder(store):
    a = store.ensure_agent("a")
    o = Orchestrator(store)
    j = o.submit("x", dag=[{"key": "../../mnt/host/evil", "title": "t", "assign": "a"}])
    sid = j["subtasks"][0]["id"]
    o.claim(a, sid)
    o.complete(sid, a, "done", changes={"diff": DIFF})
    paths = [r["path"] for r in store.q("SELECT path FROM drive_files")]
    assert paths and all(p.startswith(f"/jobs/{j['job_id']}/") for p in paths), paths
