"""Skill discovery against a mocked GitHub REST API + local skill import."""
import base64
import http.server
import json
import threading
from urllib.parse import parse_qs, urlparse

import pytest

from commons_memory.skilldiscovery import Discovery
from commons_memory.skillimport import run as import_local
from commons_memory.skillpool import SkillPool
from commons_memory.store import CommonsError

STATE = {"stars": 100, "sha": "a" * 40, "body": "---\nname: pdf-tools\ndescription: Work with PDFs\n---\n# PDF\nUse pdftotext.\n",
         "calls": 0, "remaining": 50}
REPOS = [{"full_name": "acme/skills", "name": "skills", "description": "Agent skills", "html_url": "https://github.com/acme/skills",
          "default_branch": "main", "forks_count": 7, "pushed_at": "2026-10-10T00:00:00Z", "license": {"spdx_id": "MIT"},
          "topics": ["agent-skills"]},
         {"full_name": "evil/gpl", "name": "gpl", "description": "GPL skills", "html_url": "https://github.com/evil/gpl",
          "default_branch": "main", "forks_count": 1, "pushed_at": "2025-01-01T00:00:00Z", "license": {"spdx_id": "GPL-3.0"},
          "topics": []}]


class GH(http.server.BaseHTTPRequestHandler):
    def _j(self, obj, code=200):
        STATE["calls"] += 1
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-RateLimit-Remaining", str(STATE["remaining"]))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        assert self.headers.get("Authorization") == "Bearer ghp_test_token"
        if u.path == "/search/repositories":
            items = [dict(r, stargazers_count=STATE["stars"] if r["name"] == "skills" else 5) for r in REPOS]
            return self._j({"items": items})
        if u.path.endswith("/git/trees/main"):
            return self._j({"tree": [{"path": "pdf-tools/SKILL.md", "type": "blob", "sha": "b1"},
                                     {"path": "README.md", "type": "blob", "sha": "b2"},
                                     {"path": ".cursor/rules/style.mdc", "type": "blob", "sha": "b3"}]})
        if u.path.endswith("/commits"):
            return self._j([{"sha": STATE["sha"]}])
        if "/contents/" in u.path:
            assert q.get("ref") == [STATE["sha"]]
            return self._j({"encoding": "base64", "content": base64.b64encode(STATE["body"].encode()).decode(), "sha": "x"})
        self._j({"message": "not found"}, 404)

    def log_message(self, *a):
        pass


@pytest.fixture
def gh(store, monkeypatch):
    srv = http.server.HTTPServer(("127.0.0.1", 0), GH)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
    monkeypatch.delenv("COMMONS_SKILL_AUTO_APPROVE", raising=False)
    d = Discovery(store, api=f"http://127.0.0.1:{srv.server_port}")
    d.configure("env://GITHUB_TOKEN")
    yield d
    srv.shutdown()


def test_discover_rank_add_update(gh, store):
    r = gh.discover(per_query=5)
    assert r["repos"] == 2 and r["added"] >= 3
    ids = {x["id"] for x in gh.list("popular")}
    assert "acme/skills:pdf-tools/SKILL.md" in ids and "acme/skills:.cursor/rules/style.mdc" in ids
    # star velocity: simulate a day passing with +40 stars
    store.x("UPDATE skill_catalog_history SET ts=ts-86400*2")
    STATE["stars"] = 140
    gh.discover(per_query=5)
    top = gh.list("trending")[0]
    assert top["repo"] == "acme/skills" and top["delta_7d"] == 40
    # import pinned version via the gate: pending, never auto-active
    res = gh.add("acme/skills:pdf-tools/SKILL.md")
    assert res["status"] == "pending" and STATE["sha"] in res["source"]
    assert SkillPool(store).history("pdf-tools")[0]["status"] == "pending"
    # license allow-list
    with pytest.raises(CommonsError):
        gh.add("evil/gpl:pdf-tools/SKILL.md")
    # upstream change -> new pending version with diff
    STATE["sha"], STATE["body"] = "c" * 40, STATE["body"] + "Also use qpdf.\n"
    up = gh.check_updates()["updates"]
    assert up and up[0]["new_version"] == 2
    h = SkillPool(store).history("pdf-tools")
    assert "+Also use qpdf." in h[-1]["diff"] and h[-1]["status"] == "pending"


def test_rate_limit_stops_early(gh):
    STATE["remaining"] = 1
    r = gh.discover(per_query=5)
    STATE["remaining"] = 50
    assert r["errors"] and "rate limit" in r["errors"][0] and r["api_calls"] == 1


def test_token_must_be_ref(store):
    with pytest.raises(CommonsError):
        Discovery(store).configure("ghp_raw")


def test_import_local_dedupe_and_replace(store, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    sk = tmp_path / "home/.claude/skills/deploy-checks"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: deploy-checks\ndescription: Pre-deploy checklist\n---\nRun tests first.\n")
    (sk / "check.sh").write_text("#!/bin/sh\nmake test\n")
    proj = tmp_path / "proj"
    (proj / ".cursor/rules").mkdir(parents=True)
    (proj / ".cursor/rules/style.mdc").write_text("Use 4-space indents.")
    (proj / "AGENTS.md").write_text("# Agents\nAlways run lint.")
    dry = import_local(store, str(proj), dry_run=True)
    assert {x["name"] for x in dry["imported"]} == {"deploy-checks", "cursor-style", "agents-md-proj"}
    r = import_local(store, str(proj), replace_local=True)
    assert len(r["imported"]) == 3 and r["replaced"]
    assert SkillPool(store).history("deploy-checks")[0]["status"] in ("pending", "active")
    assert "Commons pool" in (sk / "SKILL.md").read_text()
    again = import_local(store, str(proj))
    assert not again["imported"] and len(again["duplicates"]) == 2  # pointer itself is skipped
    hist = SkillPool(store).history("deploy-checks")
    assert "imported from local skill" in hist[0]["changelog"]
