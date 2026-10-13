import base64
import json
import os

import pytest

from commons_memory import events as EV
from commons_memory import settings
from commons_memory.store import CommonsError


def test_webhook_turns_events_into_jobs_and_checks_its_token(store):
    tok = EV.hook_token(store)
    with pytest.raises(CommonsError):
        EV.inbound(store, "wrong", {"title": "x"})
    j = EV.inbound(store, tok, {"title": "Disk 3 is failing on the NAS", "body": "SMART reallocated sectors rising"})
    assert store.q("SELECT status FROM jobs WHERE id=?", (j["job_id"],))[0]["status"] == "running"
    gh = {"repository": {"full_name": "o/r"}, "workflow_run": {"conclusion": "failure", "head_branch": "main",
                                                              "name": "tests", "html_url": "https://x"}}
    assert "job_id" in EV.inbound(store, tok, gh, "workflow_run")
    assert EV.inbound(store, tok, {"action": "closed"}, "issues") == {"ignored": "issues"}


def test_github_watcher_skips_the_backlog_then_reports_new_events(store, monkeypatch):
    from commons_memory import vault as V
    monkeypatch.setenv("COMMONS_VAULT_KEY", base64.b64encode(os.urandom(32)).decode())
    V.add(store, "gh", "ghp_test_token_123456", "github", ["https://api.github.com"], {"bearer": True})
    settings.put(store, "github_watch", "o/r")
    feed = {"ci": [{"id": 1, "head_branch": "main", "name": "tests", "html_url": "u1", "head_sha": "abc"}],
            "issue": [{"id": 10, "number": 3, "title": "old bug", "html_url": "u2", "user": {"login": "x"}}]}
    def fake(s, cred, method, url, body="", content_type="", timeout=30):
        if "/actions/runs" in url:
            return {"status": 200, "body": json.dumps({"workflow_runs": feed["ci"]})}
        if "/issues" in url:
            return {"status": 200, "body": json.dumps(feed["issue"])}
        return {"status": 200, "body": "[]"}
    monkeypatch.setattr(V, "call", fake)
    assert EV.watch_github(store) == []                                       # first look: backlog only marked
    feed["issue"].append({"id": 11, "number": 4, "title": "new crash on login", "html_url": "u3", "user": {"login": "y"}})
    made = EV.watch_github(store)
    assert len(made) == 1
    assert "new crash on login" in store.q("SELECT title FROM jobs WHERE id=?", (made[0],))[0]["title"]
    assert EV.watch_github(store) == []                                       # never twice


def test_hook_endpoint_works_over_http_without_an_agent_key_but_needs_its_token(store):
    from starlette.testclient import TestClient
    from commons_memory.agent_http import build_app
    from commons_memory.auth import AuthMiddleware
    c = TestClient(AuthMiddleware(build_app(store), store, resource_url="http://testserver", allow_anon_reads=False))
    assert c.get("/agent/presence").status_code == 401                         # everything else still needs a key
    assert c.post("/hooks/not-the-token", json={"title": "x"}).status_code == 403
    r = c.post(f"/hooks/{EV.hook_token(store)}", json={"title": "Backup failed on the NAS", "body": "rc=1"})
    assert r.status_code == 200 and r.json()["job_id"]
