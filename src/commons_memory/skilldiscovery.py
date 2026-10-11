"""Skill discovery: find public agent skills on GitHub via the official REST API (no HTML scraping).

* Auth: a secret ref (e.g. env://GITHUB_TOKEN) set with `configure(token_ref=...)`; unauthenticated works at low rate.
* Respects rate limits (X-RateLimit-Remaining / Retry-After) and stops early instead of hammering.
* Finds SKILL.md skills, Claude Code plugins/skill repos, AGENTS.md / Cursor-rules collections and awesome-lists.
* Ranks by stars, star velocity (1d / 7d deltas from daily snapshots), forks and recency.
* Catalog entries are metadata only (source URL, license, pinned commit sha) — never auto-activated. `add()` imports a
  pinned version through the skill pool's review gate (license allow-list, secret/injection scan, provenance);
  `check_updates()` turns upstream changes into new *pending* versions with diffs that need re-approval.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .ids import iso, now
from .store import CommonsError, Store

API = os.environ.get("COMMONS_GITHUB_API", "https://api.github.com")
OK_LICENSES = {"mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc", "cc0-1.0", "cc-by-4.0", "unlicense",
               "mpl-2.0", "0bsd", "zlib"}
QUERIES = [  # (query, kind)
    ("topic:agent-skills", "skill"), ("topic:claude-skills", "skill"), ("topic:claude-code-plugin", "plugin"),
    ("topic:claude-code-skills", "skill"), ('"SKILL.md" in:readme claude', "skill"), ("topic:cursor-rules", "rules"),
    ('"AGENTS.md" in:name,description', "agents_md"), ("awesome claude skills in:name,description", "list"),
    ("awesome agent skills in:name,description", "list")]
SKILL_FILE = re.compile(r"(^|/)(SKILL\.md|AGENTS\.md)$|(^|/)\.cursor/rules/[^/]+\.mdc$|\.cursorrules$")


class RateLimited(Exception):
    pass


class Discovery:
    def __init__(self, store: Store, api: str | None = None):
        self.s = store
        self.api = (api or os.environ.get("COMMONS_GITHUB_API") or API).rstrip("/")
        self.calls = 0

    # ------------------------------------------------------------------ config / http
    def configure(self, token_ref: str | None = None) -> dict:
        if token_ref:
            if "://" not in token_ref:
                raise CommonsError("token must be a secret ref like env://GITHUB_TOKEN")
            from .secrets import Broker
            if not self.s.q("SELECT 1 FROM secret_refs WHERE ref=?", (token_ref,)):
                Broker(self.s).register(token_ref, "GitHub API token for skill discovery (read-only, public repos)")
            self.s.x("INSERT OR REPLACE INTO meta VALUES('github_token_ref', ?)", (token_ref,))
        r = self.s.q("SELECT value FROM meta WHERE key='github_token_ref'")
        return {"token_ref": r[0]["value"] if r else None, "api": self.api}

    def _token(self) -> str | None:
        r = self.s.q("SELECT value FROM meta WHERE key='github_token_ref'")
        if not r:
            return None
        from .secrets import Broker
        try:
            return Broker(self.s).resolve_internal(r[0]["value"], "skill-discovery")
        except Exception:  # noqa: BLE001
            return None

    def _get(self, path: str, params: dict | None = None):
        url = f"{self.api}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
             "User-Agent": "commons-memory-skill-discovery"}
        tok = self._token()
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        self.calls += 1
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as resp:  # noqa: S310
                rem = resp.headers.get("X-RateLimit-Remaining")
                data = json.loads(resp.read() or b"null")
                if rem is not None and int(rem) <= 2:
                    raise RateLimited(f"rate limit nearly exhausted (reset {resp.headers.get('X-RateLimit-Reset')})")
                return data
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and (e.headers.get("X-RateLimit-Remaining") == "0" or e.headers.get("Retry-After")):
                raise RateLimited(f"GitHub rate limit ({e.code}); retry after {e.headers.get('Retry-After') or 'reset'}") from None
            raise

    # ------------------------------------------------------------------ discover
    def discover(self, max_repos: int = 60, per_query: int = 20, scan_trees: bool = True) -> dict:
        seen, added, updated, errors = {}, 0, 0, []
        try:
            for q, kind in QUERIES:
                data = self._get("/search/repositories", {"q": q, "sort": "stars", "order": "desc", "per_page": per_query})
                for repo in (data or {}).get("items", []):
                    seen.setdefault(repo["full_name"], (repo, kind))
                if len(seen) >= max_repos:
                    break
            for full, (repo, kind) in list(seen.items())[:max_repos]:
                self.s.x("INSERT INTO skill_catalog_history VALUES(?,?,?,?)",
                         (full, now(), repo.get("stargazers_count", 0), repo.get("forks_count", 0)))
                files = []
                if scan_trees:
                    try:
                        br = repo.get("default_branch") or "main"
                        tree = self._get(f"/repos/{full}/git/trees/{urllib.parse.quote(br)}", {"recursive": "1"})
                        files = [t for t in (tree or {}).get("tree", []) if t.get("type") == "blob" and
                                 SKILL_FILE.search(t["path"])][:40]
                    except RateLimited:
                        raise
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"{full}: {e}")
                entries = [(f["path"], f.get("sha"), "skill" if f["path"].endswith("SKILL.md") else
                            "agents_md" if f["path"].endswith("AGENTS.md") else "rules") for f in files] \
                    or [("", None, kind)]
                for path, sha, k in entries:
                    cid = f"{full}:{path}" if path else full
                    name = (path.rsplit("/", 2)[-2] if path.endswith("SKILL.md") and "/" in path else
                            repo["name"]) if path else repo["name"]
                    lic = ((repo.get("license") or {}).get("spdx_id") or "NOASSERTION")
                    exists = self.s.q("SELECT 1 FROM skill_catalog WHERE id=?", (cid,))
                    self.s.x("INSERT INTO skill_catalog(id,repo,path,name,description,url,license,sha,stars,forks,pushed_at,"
                             "kind,topics,first_seen,last_seen,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'new') "
                             "ON CONFLICT(id) DO UPDATE SET stars=excluded.stars, forks=excluded.forks, "
                             "pushed_at=excluded.pushed_at, last_seen=excluded.last_seen, description=excluded.description, "
                             "license=excluded.license, sha=COALESCE(excluded.sha, skill_catalog.sha)",
                             (cid, full, path, _slug(name), (repo.get("description") or "")[:300],
                              repo.get("html_url", "") + (f"/blob/{repo.get('default_branch', 'main')}/{path}" if path else ""),
                              lic, sha, repo.get("stargazers_count", 0), repo.get("forks_count", 0),
                              repo.get("pushed_at"), k, json.dumps(repo.get("topics") or []), now(), now()))
                    updated += bool(exists)
                    added += not exists
        except RateLimited as e:
            errors.append(str(e))
        self.s.x("INSERT OR REPLACE INTO meta VALUES('skill_discovery_last', ?)",
                 (json.dumps({"ts": now(), "repos": len(seen), "added": added, "errors": errors[:5], "calls": self.calls}),))
        self.s.audit(None, "skills.discover", f"{len(seen)} repos", {"added": added, "calls": self.calls})
        return {"repos": len(seen), "added": added, "updated": updated, "api_calls": self.calls, "errors": errors[:10]}

    # ------------------------------------------------------------------ ranking
    def _delta(self, repo: str, days: float) -> int:
        cur = self.s.q("SELECT stars FROM skill_catalog_history WHERE repo=? ORDER BY ts DESC LIMIT 1", (repo,))
        old = self.s.q("SELECT stars FROM skill_catalog_history WHERE repo=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                       (repo, now() - days * 86400 + 3600))
        if not old:  # history shorter than the window: compare with the earliest snapshot
            old = self.s.q("SELECT stars FROM skill_catalog_history WHERE repo=? AND ts<? ORDER BY ts LIMIT 1",
                           (repo, now() - 3600))
        if not cur or not old:
            return 0
        return cur[0]["stars"] - old[0]["stars"]

    def list(self, mode: str = "trending", query: str = "", kind: str = "", limit: int = 50) -> list[dict]:
        rows = [dict(r) for r in self.s.q("SELECT * FROM skill_catalog")]
        q = query.lower()
        out = []
        for r in rows:
            if q and q not in f"{r['name']} {r['description']} {r['repo']} {r['topics']}".lower():
                continue
            if kind and r["kind"] != kind:
                continue
            d1, d7 = self._delta(r["repo"], 1), self._delta(r["repo"], 7)
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(r["pushed_at"].replace("Z", "+00:00"))).days
            except Exception:  # noqa: BLE001
                age = 9999
            r.update(delta_1d=d1, delta_7d=d7, days_since_push=age, topics=json.loads(r["topics"] or "[]"),
                     first_seen=iso(r["first_seen"]), last_seen=iso(r["last_seen"]),
                     license_ok=(r["license"] or "").lower() in OK_LICENSES)
            r["score"] = round(d7 * 3 + d1 * 5 + (r["forks"] or 0) * 0.05 + max(0, 30 - age) * 0.2, 2) if mode == "trending" \
                else (r["stars"] or 0) + (r["forks"] or 0) * 0.5
            out.append(r)
        out.sort(key=lambda r: (-r["score"], -(r["stars"] or 0)))
        return out[:limit]

    # ------------------------------------------------------------------ import into the pool
    def _content(self, repo: str, path: str, ref: str | None) -> tuple[str, str]:
        params = {"ref": ref} if ref else None
        d = self._get(f"/repos/{repo}/contents/{urllib.parse.quote(path)}", params)
        body = base64.b64decode(d["content"]).decode("utf-8", "replace") if d.get("encoding") == "base64" else d.get("content", "")
        return body, d.get("sha") or ref or ""

    def _commit(self, repo: str, path: str) -> str | None:
        c = self._get(f"/repos/{repo}/commits", {"path": path, "per_page": 1})
        return c[0]["sha"] if c else None

    def add(self, cid: str, name: str | None = None, agent_id: str | None = None, allow_license: bool = False) -> dict:
        r = self.s.q("SELECT * FROM skill_catalog WHERE id=?", (cid,))
        if not r:
            raise CommonsError(f"unknown catalog entry {cid}; run discovery first")
        e = dict(r[0])
        if not e["path"]:
            raise CommonsError("this entry is a repo/list without a skill file; pick one of its SKILL.md entries")
        if (e["license"] or "").lower() not in OK_LICENSES and not allow_license:
            raise CommonsError(f"license {e['license']} is not on the allow-list ({', '.join(sorted(OK_LICENSES))}); "
                               "pass allow_license after checking the terms")
        commit = self._commit(e["repo"], e["path"]) or e["sha"]
        body, _ = self._content(e["repo"], e["path"], commit)
        content = _as_skill(body, name or e["name"], e)
        from .skillpool import SkillPool
        pool = SkillPool(self.s, require_human=True)
        skill = name or e["imported_skill"] or e["name"]
        prov = (f"imported from https://github.com/{e['repo']}/blob/{commit}/{e['path']} "
                f"(license {e['license']}, commit {commit})")
        if self.s.q("SELECT 1 FROM skills WHERE name=?", (skill,)):
            res = pool.update(skill, content, changelog=prov, agent_id=agent_id)
        else:
            res = pool.publish(skill, content, description=e["description"], tags=["imported", "github", e["kind"]],
                               agent_id=agent_id, changelog=prov)
        self.s.x("UPDATE skill_catalog SET imported_skill=?, imported_sha=?, status='imported' WHERE id=?", (skill, commit, cid))
        sk = self.s.q("SELECT id FROM skills WHERE name=?", (skill,))[0]["id"]
        self.s.relate(self.s.upsert_entity("skill", skill), self.s.upsert_entity("source_repo", e["repo"]), "imported_from")
        self.s.audit(agent_id, "skills.import.github", cid, {"skill": skill, "commit": commit, "skill_id": sk})
        return res | {"source": prov}

    def check_updates(self, agent_id: str | None = None) -> dict:
        out = []
        for e in self.s.q("SELECT * FROM skill_catalog WHERE imported_skill IS NOT NULL"):
            try:
                head = self._commit(e["repo"], e["path"])
            except RateLimited as ex:
                out.append({"id": e["id"], "error": str(ex)})
                break
            if head and head != e["imported_sha"]:
                body, _ = self._content(e["repo"], e["path"], head)
                from .skillpool import SkillPool
                res = SkillPool(self.s, require_human=True).update(
                    e["imported_skill"], _as_skill(body, e["imported_skill"], dict(e)), agent_id=agent_id,
                    changelog=f"upstream update {e['imported_sha'][:7]}→{head[:7]} from github.com/{e['repo']}")
                self.s.x("UPDATE skill_catalog SET imported_sha=?, status='update_pending' WHERE id=?", (head, e["id"]))
                out.append({"id": e["id"], "skill": e["imported_skill"], "new_version": res["version"], "status": res["status"]})
        return {"updates": out}

    def due(self, every_h: float = 24) -> bool:
        r = self.s.q("SELECT value FROM meta WHERE key='skill_discovery_last'")
        return not r or now() - json.loads(r[0]["value"])["ts"] >= every_h * 3600

    def status(self) -> dict:
        r = self.s.q("SELECT value FROM meta WHERE key='skill_discovery_last'")
        last = json.loads(r[0]["value"]) if r else None
        if last:
            last["ts"] = iso(last["ts"])
        return {"last_run": last, **self.configure(), "entries": self.s.q("SELECT COUNT(*) c FROM skill_catalog")[0]["c"]}


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", (s or "skill").lower()).strip("-")
    return (s or "skill")[:60] if re.match(r"[a-z0-9]", s or "") else ("s-" + s)[:60]


def _as_skill(body: str, name: str, meta: dict) -> str:
    """Normalize to Agent Skills format; AGENTS.md / cursor rules get wrapped with front matter."""
    if body.lstrip().startswith("---") and re.search(r"(?m)^name:", body.split("---", 2)[1] if body.count("---") >= 2 else ""):
        return body
    desc = (meta.get("description") or f"Imported from {meta.get('repo') or meta.get('path')}").replace("\n", " ")[:200]
    body = re.sub(r"^---.*?---\s*", "", body, flags=re.S) if body.lstrip().startswith("---") else body
    return f"---\nname: {_slug(name)}\ndescription: {desc}\n---\n{body}"


def rate_info() -> str:
    return time.strftime("%H:%M")
