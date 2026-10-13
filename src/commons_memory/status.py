"""Status cards: what each agent is doing right now, in one line (working / blocked / done / needs you), shown as
"Team now" on the dashboard. start_work, finish_work and Needs-you questions set it automatically; agents can post
their own with status_update."""
from __future__ import annotations

import json

from .ids import iso, now
from .store import CommonsError, Store

SCHEMA = """CREATE TABLE IF NOT EXISTS agent_status(agent_id TEXT PRIMARY KEY, state TEXT, headline TEXT, bullets TEXT,
  updated_at REAL);"""
STATES = ("working", "blocked", "done", "needs_you", "idle")
SYSTEM = ("cli", "user", "librarian", "commons", "dashboard-user", "reviewer")


def migrate(db) -> None:
    db.executescript(SCHEMA)


def put(s: Store, agent_id: str, state: str, headline: str, bullets: list[str] | None = None) -> dict:
    if state not in STATES:
        raise CommonsError(f"state is one of {', '.join(STATES)}")
    from .redact import redact
    headline = redact(" ".join((headline or "").split()))[0][:90]
    bl = [redact(" ".join(str(b).split()))[0][:100] for b in (bullets or [])][:5]
    s.x("INSERT OR REPLACE INTO agent_status VALUES(?,?,?,?,?)", (agent_id, state, headline, json.dumps(bl), now()))
    return {"state": state, "headline": headline, "bullets": bl}


def team(s: Store) -> list[dict]:
    from .messaging import presence
    out = []
    for a in s.q("SELECT id, name FROM agents ORDER BY name"):
        if a["name"] in SYSTEM:
            continue
        st = s.q("SELECT * FROM agent_status WHERE agent_id=?", (a["id"],))
        p = presence(s, a["id"])
        if not st and not p.get("last_seen"):
            continue  # never connected, nothing to show
        work = s.q("SELECT what FROM work_claims WHERE agent_id=? AND status='active' ORDER BY started_at DESC LIMIT 1",
                   (a["id"],))
        row = dict(st[0]) if st else {}
        state = row.get("state") or ("working" if work else "idle")  # an open work claim means working
        out.append({"agent": a["name"], "state": state, "headline": row.get("headline") or "",
                    "bullets": json.loads(row.get("bullets") or "[]"), "since": iso(row["updated_at"]) if row else None,
                    "live": p["wake_mode"] in ("instant", "long-poll", "push"), "wake_mode": p["wake_mode"],
                    "working_on": work[0]["what"] if work else None})
    order = {"needs_you": 0, "blocked": 1, "working": 2, "done": 3, "idle": 4}
    return sorted(out, key=lambda x: (order.get(x["state"], 5), x["agent"]))


def git_verify(s: Store, repo: str, branch: str, commit: str) -> str:
    """Check a handoff's commit on GitHub through the vault's GitHub key (the agent never sees it)."""
    import re
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit or ""):
        return "unverified: not a commit hash"
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo or ""):
        return "unverified: repo must be owner/name"
    from . import vault as V
    cred = s.q("SELECT * FROM vault_creds WHERE service='github' AND secret IS NOT NULL ORDER BY name LIMIT 1")
    if not cred:
        return "unverified: no GitHub key in the vault"
    cred = dict(cred[0])
    r = V.call(s, cred, "GET", f"https://api.github.com/repos/{repo}/commits/{commit}")
    if r.get("status") != 200:
        return f"unverified: GitHub says {r.get('status')} for that commit"
    if branch:
        c = V.call(s, cred, "GET", f"https://api.github.com/repos/{repo}/compare/{branch}...{commit}")
        state = json.loads(c.get("body") or "{}").get("status") if c.get("status") == 200 else None
        if state not in ("identical", "behind"):
            return f"commit exists but is not on {branch} ({state or c.get('status')})"
    return "verified on GitHub" + (f" (on {branch})" if branch else "")
