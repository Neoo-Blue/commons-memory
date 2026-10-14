"""Outside events become work. A GitHub watcher pulls each watched repo through the vault's GitHub key (so it works
behind NAT, nothing exposed): a failed CI run, a new issue or a new PR becomes a job for the best live agent. A
token-protected webhook (/hooks/<token>) takes GitHub webhook payloads or {title, body} from anything that can reach
the hub. The first look at a repo only marks what is already there, so switching it on doesn't flood the agents.

Anything a stranger can write (issue and PR titles and bodies, branch names, commit messages) reaches agents only inside
an <untrusted> fence, never in a job title, and by default only people inside the repo (owner, member, collaborator)
make jobs at all: an open repo's issue tracker is a prompt anyone can type into (setting `github_outside`)."""
from __future__ import annotations

import hmac
import json
import secrets

from . import settings
from .redact import untrusted
from .store import CommonsError, Store

INSIDE = {"OWNER", "MEMBER", "COLLABORATOR"}


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


def _outside(it: dict, repo: str) -> bool:
    """An issue or PR whose author isn't owner, member or collaborator (unknown counts as outside), or a CI run of a
    fork's branch."""
    if "number" in it:
        return it.get("author_association") not in INSIDE
    head = (it.get("head_repository") or {}).get("full_name")
    return bool(head) and head != repo


def _gh_job(s: Store, repo: str, kind: str, it: dict) -> str | None:
    """One GitHub item as a job. The title carries only what Commons controls (repo, kind, number, run id); everything
    the author wrote goes in the fence."""
    if _outside(it, repo) and not settings.get(s, "github_outside"):
        s.audit(None, "event.skipped", f"{repo}:{kind}:{it.get('number') or it.get('id')}", {"why": "outside author"})
        return None
    if kind == "ci":
        title = f"GitHub {repo}: CI run {it.get('id')} failed"
        told = (f"workflow: {it.get('name')}\nbranch: {it.get('head_branch')}\ncommit {(it.get('head_sha') or '')[:12]}: "
                f"{(it.get('display_title') or '')[:200]}")
        ask = "Find the cause and fix it, or report what blocks it."
    else:
        what = "PR" if kind == "pr" else "issue"
        login = (it.get("user") or {}).get("login") or "?"
        who = login if it.get("author_association") in INSIDE else f"{login}, outside the repo"
        title = f"GitHub {repo}: {what} #{it.get('number')} by {login}"[:120]
        told = f"{(it.get('title') or '')[:200]}\n\n{(it.get('body') or '')[:1500]}"
        ask = f"Triage it ({who}): {'review it' if kind == 'pr' else 'reproduce or answer it'}, and report what you did."
    source = f"github:{repo}" + (f"#{it.get('number')}" if it.get("number") else "")
    return _job(s, title, f"{title}\n{it.get('html_url') or ''}\n{untrusted(told, source)}\n\n{ask}")


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
                j = _gh_job(s, repo, kind, it)
                if j:
                    made.append(j)
    return made


def inbound(s: Store, token: str, payload: dict, gh_event: str = "") -> dict:
    if not hmac.compare_digest(token or "", hook_token(s)):
        raise CommonsError("unknown hook")
    if gh_event:
        repo = (payload.get("repository") or {}).get("full_name", "?")
        if gh_event == "workflow_run" and (payload.get("workflow_run") or {}).get("conclusion") == "failure":
            kind, it = "ci", payload["workflow_run"]
        elif gh_event in ("issues", "pull_request") and payload.get("action") == "opened":
            kind, it = ("pr" if gh_event == "pull_request" else "issue"), payload.get("issue") or payload.get("pull_request") or {}
        else:
            return {"ignored": gh_event}
        j = _gh_job(s, repo, kind, it)
        return {"job_id": j} if j else {"ignored": "outside author"}
    title, body = str(payload.get("title") or "").strip(), str(payload.get("body") or "")
    if not title:
        raise CommonsError("send {title, body}")
    # the token holder wrote the title; the body is often relayed from elsewhere (an alert, a form), so it is fenced
    return {"job_id": _job(s, title, f"{title}\n{untrusted(body, 'hook')}" if body else title, payload.get("project"))}
