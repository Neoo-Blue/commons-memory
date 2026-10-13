"""Outside events become work. A GitHub watcher pulls each watched repo through the vault's GitHub key (so it works
behind NAT, nothing exposed): a failed CI run, a new issue or a new PR becomes a job for the best live agent. A
token-protected webhook (/hooks/<token>) takes GitHub webhook payloads or {title, body} from anything that can reach
the hub. The first look at a repo only marks what is already there, so switching it on doesn't flood the agents."""
from __future__ import annotations

import hmac
import json
import secrets

from . import settings
from .store import CommonsError, Store


def hook_token(s: Store) -> str:
    r = s.q("SELECT value FROM meta WHERE key='hook_token'")
    if r:
        return r[0]["value"]
    s.x("INSERT OR IGNORE INTO meta VALUES('hook_token', ?)", (secrets.token_urlsafe(24),))
    return s.q("SELECT value FROM meta WHERE key='hook_token'")[0]["value"]


def _job(s: Store, title: str, body: str, project: str | None = None) -> str:
    from .orchestrator import Orchestrator
    j = Orchestrator(s).submit(body, pattern="parallel", project=project, title=title[:120],
                               dag=[{"key": "only", "type": "general", "title": title[:120], "body": body, "deps": []}])
    s.audit(None, "event.job", j["job_id"], {"title": title[:120]})
    return j["job_id"]


def _seen(s: Store, key: str) -> bool:
    if s.q("SELECT 1 FROM meta WHERE key=?", (key,)):
        return True
    s.x("INSERT OR IGNORE INTO meta VALUES(?, '1')", (key,))
    return False


def watch_github(s: Store) -> list[str]:
    repos = settings.get(s, "github_watch")
    if not repos:
        return []
    cred = s.q("SELECT * FROM vault_creds WHERE service='github' AND secret IS NOT NULL ORDER BY name LIMIT 1")
    if not cred:
        return []
    from . import vault as V
    cred, made = dict(cred[0]), []
    for repo in repos:
        first = not _seen(s, f"evt:gh:init:{repo}")  # (marks the repo as initialised on this first pass)
        api = f"https://api.github.com/repos/{repo}"
        feeds = [("ci", f"{api}/actions/runs?status=failure&per_page=5", "workflow_runs"),
                 ("issue", f"{api}/issues?state=open&sort=created&direction=desc&per_page=10", None),
                 ("pr", f"{api}/pulls?state=open&sort=created&direction=desc&per_page=10", None)]
        for kind, url, field in feeds:
            r = V.call(s, cred, "GET", url)
            if r.get("status") != 200:
                continue
            data = json.loads(r["body"] or "[]")
            for it in (data.get(field, []) if field else data):
                if kind == "issue" and it.get("pull_request"):
                    continue  # the issues feed also lists PRs
                if _seen(s, f"evt:gh:{repo}:{kind}:{it['id']}") or first:
                    continue
                if kind == "ci":
                    title = f"GitHub {repo}: CI failed on {it.get('head_branch')}: {it.get('name')}"
                    body = (f"{title}\n{it.get('html_url')}\nCommit {it.get('head_sha', '')[:12]}: "
                            f"{(it.get('display_title') or '')[:200]}\nFind the cause and fix it, or report what blocks it.")
                else:
                    title = f"GitHub {repo}: new {'PR' if kind == 'pr' else 'issue'} #{it.get('number')}: {it.get('title', '')[:90]}"
                    body = (f"{title}\n{it.get('html_url')}\nBy {(it.get('user') or {}).get('login')}.\n\n"
                            f"{(it.get('body') or '')[:1500]}\n\nTriage it: {'review it' if kind == 'pr' else 'reproduce or answer it'}, "
                            "and report what you did.")
                made.append(_job(s, title, body))
    return made


def inbound(s: Store, token: str, payload: dict, gh_event: str = "") -> dict:
    if not hmac.compare_digest(token or "", hook_token(s)):
        raise CommonsError("unknown hook")
    if gh_event:
        repo = (payload.get("repository") or {}).get("full_name", "?")
        if gh_event == "workflow_run" and (payload.get("workflow_run") or {}).get("conclusion") == "failure":
            w = payload["workflow_run"]
            title, body = f"GitHub {repo}: CI failed on {w.get('head_branch')}: {w.get('name')}", w.get("html_url", "")
        elif gh_event in ("issues", "pull_request") and payload.get("action") == "opened":
            it = payload.get("issue") or payload.get("pull_request") or {}
            title = f"GitHub {repo}: new {'PR' if gh_event == 'pull_request' else 'issue'} #{it.get('number')}: {it.get('title', '')[:90]}"
            body = f"{it.get('html_url')}\n\n{(it.get('body') or '')[:1500]}"
        else:
            return {"ignored": gh_event}
    else:
        title, body = str(payload.get("title") or "").strip(), str(payload.get("body") or "")
        if not title:
            raise CommonsError("send {title, body}")
    return {"job_id": _job(s, title, f"{title}\n{body}".strip(), payload.get("project") if not gh_event else None)}
