"""Projects as people think of them: one page per product with everything the team did on it.

Agents file work under whatever name they run in (a repo, a worktree, sometimes just the home folder), so one product
ends up under several raw names, and folders show up as projects. A project here is a raw name plus the names grouped
under it (projects.parent_id), with an optional display title. A folder can be marked "not a project" (kind='folder');
a raw name with little history is a task, not a project (kind='task' or 'project' overrides the guess). Grouping only
changes what the dashboard shows together: agents keep scoping search and access by their raw names.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict

from .ids import iso, now
from .store import CommonsError, Store

COLUMNS = [("projects", "title", "TEXT"), ("projects", "parent_id", "TEXT"), ("projects", "kind", "TEXT")]
KINDS = ("project", "task", "folder")
# names that are where an agent happened to run, not what it worked on
FOLDERISH = {"home", "workspace", "tmp", "temp", "desktop", "downloads", "documents", "src", "code", "projects", "dev",
             "repos", "default", "general", "misc", "memory", "services", "scratch", "work"}
HIGHLIGHTS = ("decision", "handoff", "session", "job", "job_done", "step", "work", "work_done", "ask", "change")
EVERYTHING = HIGHLIGHTS + ("memory", "fact", "note", "observation", "document", "file")
DAY = 86400


def _rows(s: Store) -> dict[str, dict]:
    return {r["id"]: dict(r) for r in s.q("SELECT id, name, title, parent_id, kind, created_at FROM projects")}


def _groups(rows: dict[str, dict]) -> dict[str, list[str]]:
    """root id -> [root, members...]. A member whose parent vanished counts as its own root."""
    g: dict[str, list[str]] = defaultdict(list)
    for pid, r in rows.items():
        root = r["parent_id"] if r["parent_id"] in rows else pid
        g[root].append(pid)
    for root, ids in g.items():
        ids.sort(key=lambda i: i != root)
    return g


def _stats(s: Store) -> dict[str, dict]:
    """Per raw project: counts and last activity, one GROUP BY per table."""
    st: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    def add(sql, *fields, args=()):
        for r in s.q(sql, args):
            for f in fields:
                v = r[f] or 0
                st[r["p"]][f] = max(st[r["p"]][f], v) if f == "last" else st[r["p"]][f] + v
    add("SELECT project_id p, COUNT(*) items, MAX(created_at) last, SUM(CASE WHEN kind='decision' THEN 1 ELSE 0 END) "
        "decisions FROM items WHERE project_id IS NOT NULL GROUP BY project_id", "items", "last", "decisions")
    add("SELECT project_id p, COUNT(DISTINCT CAST(created_at AS INTEGER)/86400) days FROM items "
        "WHERE project_id IS NOT NULL GROUP BY project_id", "days")
    add("SELECT project_id p, COUNT(*) sessions, MAX(updated_at) last FROM sessions WHERE project_id IS NOT NULL "
        "GROUP BY project_id", "sessions", "last")
    add("SELECT project_id p, COUNT(*) jobs, SUM(CASE WHEN status IN ('running','queued','paused') THEN 1 ELSE 0 END) "
        "open_jobs, MAX(updated_at) last FROM jobs WHERE project_id IS NOT NULL GROUP BY project_id", "jobs", "open_jobs", "last")
    add("SELECT project_id p, SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) open_asks, MAX(updated_at) last FROM board "
        "WHERE project_id IS NOT NULL GROUP BY project_id", "open_asks", "last")
    add("SELECT project_id p, COUNT(*) claims, MAX(COALESCE(done_at, started_at)) last FROM work_claims "
        "WHERE project_id IS NOT NULL GROUP BY project_id", "claims", "last")
    add("SELECT project_id p, SUM(cost) cost FROM usage WHERE project_id IS NOT NULL AND ts>=? GROUP BY project_id",
        "cost", args=(now() - 30 * DAY,))
    return st


def _sum(st: dict, ids: list[str]) -> dict:
    out: dict = defaultdict(float)
    for i in ids:
        for k, v in st.get(i, {}).items():
            out[k] = max(out[k], v) if k == "last" else out[k] + v
    return {k: (round(v, 4) if k == "cost" else v if k == "last" else int(v)) for k, v in out.items()}


def _kind(r: dict, agg: dict) -> str:
    if r["kind"] in KINDS:
        return r["kind"]
    if r["name"].lower() in FOLDERISH:
        return "folder"
    return "project" if agg.get("days", 0) >= 2 or agg.get("sessions", 0) + agg.get("jobs", 0) + agg.get("claims", 0) >= 2 \
        else "task"


def _people(s: Store, ids: list[str], limit: int = 12) -> list[dict]:
    ph = ",".join("?" * len(ids))
    who: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    for sql, field in ((f"SELECT agent_id a, COUNT(*) n, MAX(created_at) last FROM items WHERE project_id IN ({ph}) "
                        "AND kind<>'decision' GROUP BY agent_id", "memories"),
                       (f"SELECT agent_id a, COUNT(*) n, MAX(created_at) last FROM items WHERE project_id IN ({ph}) "
                        "AND kind='decision' GROUP BY agent_id", "decisions"),
                       (f"SELECT agent_id a, COUNT(*) n, MAX(updated_at) last FROM sessions WHERE project_id IN ({ph}) "
                        "GROUP BY agent_id", "sessions"),
                       (f"SELECT t.claimed_by a, COUNT(*) n, MAX(t.updated_at) last FROM subtasks t JOIN jobs j ON j.id=t.job_id "
                        f"WHERE j.project_id IN ({ph}) AND t.status='done' GROUP BY t.claimed_by", "steps"),
                       (f"SELECT agent_id a, COUNT(*) n, MAX(COALESCE(done_at, started_at)) last FROM work_claims "
                        f"WHERE project_id IN ({ph}) GROUP BY agent_id", "claims")):
        for r in s.q(sql, ids):
            if r["a"]:
                who[r["a"]][field] += r["n"]
                who[r["a"]]["last"] = max(who[r["a"]]["last"], r["last"] or 0)
    out = [{"agent": s.label(a)[1], **{k: int(v) for k, v in d.items() if k != "last"}, "last": iso(d["last"]) if d["last"] else None,
            "_t": d["last"]} for a, d in who.items()]
    out.sort(key=lambda x: -x["_t"])
    return [{k: v for k, v in x.items() if k != "_t"} for x in out[:limit]]


def suggestions(s: Store, rows: dict[str, dict] | None = None, st: dict | None = None) -> list[dict]:
    """Groups worth a one-tap confirm: top-level names sharing a first word ("commons", "commons-dev", "commons-live").
    The page lets you untick the ones that don't belong; a dismissed suggestion stays dismissed."""
    rows = rows or _rows(s)
    st = st if st is not None else _stats(s)
    gone = {r["key"][len("projsugg:"):] for r in s.q("SELECT key FROM meta WHERE key LIKE 'projsugg:%'")}
    roots = [r for r in rows.values() if not (r["parent_id"] in rows) and r["kind"] != "folder"]
    by_word: dict[str, list[dict]] = defaultdict(list)
    for r in roots:
        w = re.split(r"[-_. ]", r["name"].lower())[0]
        if len(w) >= 4 and r["name"].lower() not in FOLDERISH and _sum(st, [r["id"]]):
            by_word[w].append(r)
    out = []
    for w, rs in sorted(by_word.items()):
        if len(rs) < 2:
            continue
        rs.sort(key=lambda r: -_sum(st, [r["id"]]).get("items", 0))
        key = "g:" + hashlib.sha1(",".join(sorted(r["id"] for r in rs)).encode()).hexdigest()[:12]
        if key in gone:
            continue
        out.append({"key": key, "kind": "group", "title": rs[0]["title"] or w.capitalize(), "into": rs[0]["id"],
                    "members": [{"id": r["id"], "name": r["name"], "items": _sum(st, [r["id"]]).get("items", 0)} for r in rs]})
    return out


def listing(s: Store) -> dict:
    """Projects (newest activity first), one-off tasks, folders that aren't projects, and grouping suggestions."""
    rows, st = _rows(s), _stats(s)
    top: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))  # raw project -> agent -> items
    for r in s.q("SELECT project_id p, agent_id a, COUNT(*) n FROM items WHERE project_id IS NOT NULL AND agent_id IS NOT NULL "
                 "GROUP BY project_id, agent_id"):
        top[r["p"]][r["a"]] += r["n"]
    projects, tasks, folders = [], [], []
    for root, ids in _groups(rows).items():
        r, agg = rows[root], _sum(st, ids)
        if not agg:
            continue  # a name nobody ever wrote anything under
        e = {"id": root, "name": r["name"], "title": r["title"] or r["name"], "kind": _kind(r, agg),
             "members": [rows[i]["name"] for i in ids], "last": iso(agg["last"]) if agg.get("last") else None,
             "counts": {k: agg.get(k, 0) for k in ("items", "decisions", "sessions", "jobs", "open_jobs", "open_asks", "claims")},
             "cost_30d": agg.get("cost", 0)}
        who: dict[str, float] = defaultdict(float)
        for i in ids:
            for ag, n in top[i].items():
                who[ag] += n
        e["agents"] = [s.label(ag)[1] for ag, _ in sorted(who.items(), key=lambda kv: -kv[1])[:4]]
        (projects if e["kind"] == "project" else tasks if e["kind"] == "task" else folders).append(e)
    for x in (projects, tasks, folders):
        x.sort(key=lambda e: e["last"] or "", reverse=True)
    loose = [{"id": j["id"], "title": j["title"], "status": j["status"], "last": iso(j["updated_at"]), "kind": "job"}
             for j in s.q("SELECT id, title, status, updated_at FROM jobs WHERE project_id IS NULL ORDER BY updated_at DESC LIMIT 50")]
    return {"projects": projects, "tasks": tasks, "jobs": loose, "folders": folders,
            "suggestions": suggestions(s, rows, st)}


def _root(pid: str, rows: dict[str, dict]) -> str:
    if pid not in rows:
        pid = next((i for i, r in rows.items() if r["name"] == pid), "")
        if not pid:
            raise CommonsError("unknown project")
    p = rows[pid]["parent_id"]
    return p if p in rows else pid


def detail(s: Store, pid: str) -> dict:
    """Everything a project page shows above the timeline."""
    rows = _rows(s)
    root = _root(pid, rows)
    ids = _groups(rows)[root]
    names = [rows[i]["name"] for i in ids]
    st = _stats(s)
    agg = _sum(st, ids)
    ph = ",".join("?" * len(ids))
    def items(kind, n):
        return [{"id": r["id"], "title": r["title"] or (r["summary"] or r["content"] or "")[:160], "agent": s.label(r["agent_id"])[1]
                 if r["agent_id"] else None, "at": iso(r["created_at"])}
                for r in s.q(f"SELECT id, title, summary, content, agent_id, created_at FROM items WHERE project_id IN ({ph}) "
                             f"AND kind=? AND superseded_by IS NULL ORDER BY created_at DESC LIMIT ?", ids + [kind, n])]
    jobs = []
    for j in s.q(f"SELECT id, title, status, pattern, created_at, updated_at FROM jobs WHERE project_id IN ({ph}) "
                 "ORDER BY updated_at DESC LIMIT 30", ids):
        c = s.q("SELECT COUNT(*) n, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) d FROM subtasks WHERE job_id=?", (j["id"],))[0]
        jobs.append({"id": j["id"], "title": j["title"], "status": j["status"], "pattern": j["pattern"],
                     "steps": c["n"] or 0, "done": c["d"] or 0, "created_at": iso(j["created_at"]), "updated_at": iso(j["updated_at"])})
    claims = [{"id": r["id"], "what": r["what"], "agent": s.label(r["agent_id"])[1] if r["agent_id"] else None,
               "status": r["status"], "result": (r["result"] or "")[:300], "at": iso(r["done_at"] or r["started_at"])}
              for r in s.q(f"SELECT * FROM work_claims WHERE project_id IN ({ph}) ORDER BY COALESCE(done_at, started_at) DESC "
                           "LIMIT 20", ids)]
    asks = [{"id": r["id"], "kind": r["kind"], "text": r["text"], "status": r["status"], "at": iso(r["created_at"])}
            for r in s.q(f"SELECT id, kind, text, status, created_at FROM board WHERE project_id IN ({ph}) AND status='open' "
                         "ORDER BY created_at DESC LIMIT 20", ids)]
    return {"id": root, "name": rows[root]["name"], "title": rows[root]["title"] or rows[root]["name"],
            "kind": _kind(rows[root], agg), "kind_set": rows[root]["kind"],
            "members": [{"id": i, "name": rows[i]["name"], "items": _sum(st, [i]).get("items", 0)} for i in ids],
            "first": iso(min(rows[i]["created_at"] or now() for i in ids)), "last": iso(agg["last"]) if agg.get("last") else None,
            "counts": {k: agg.get(k, 0) for k in ("items", "decisions", "sessions", "jobs", "open_jobs", "open_asks", "claims")}
            | {"changes": _change_count(s, names, ids)},
            "cost_30d": agg.get("cost", 0), "people": _people(s, ids),
            "decisions": items("decision", 8), "handoffs": items("handoff", 5), "jobs": jobs, "claims": claims, "asks": asks,
            "changes": changes(s, names, ids, limit=10)}


def _change_where(s: Store, names: list[str], ids: list[str]) -> tuple[str, list]:
    """Recorded changes filed under the project's names, or made inside one of its jobs."""
    jobs = [r["id"] for r in s.q(f"SELECT id FROM jobs WHERE project_id IN ({','.join('?' * len(ids))})", ids)]
    w = f"project IN ({','.join('?' * len(names))})" + "".join(" OR path LIKE ?" for _ in jobs)
    return f"({w})", names + [f"/jobs/{j}/%" for j in jobs]


def _change_count(s: Store, names: list[str], ids: list[str]) -> int:
    w, a = _change_where(s, names, ids)
    return s.q(f"SELECT COUNT(*) n FROM drive_files WHERE path LIKE '%/changes.json' AND deleted_at IS NULL AND {w}", a)[0]["n"]


def changes(s: Store, names: list[str], ids: list[str], limit: int = 20, diff_chars: int = 4000) -> list[dict]:
    """Recorded diffs and commits for the project, newest first (diff_chars=0: without the diff text)."""
    from .workspace import recent
    return recent(s, limit=limit, diff_chars=diff_chars, where=_change_where(s, names, ids))


def timeline(s: Store, pid: str, before: float | None = None, limit: int = 60, types: str = "highlights",
             agent: str | None = None) -> dict:
    """The project's history, newest first, paged by time: decisions, handoffs, sessions, jobs and their steps, claimed
    work, board items and recorded changes ("highlights"), plus memories, observations and files with types=all."""
    rows = _rows(s)
    ids = _groups(rows)[_root(pid, rows)]
    names = [rows[i]["name"] for i in ids]
    want = set(HIGHLIGHTS if types == "highlights" else EVERYTHING if types == "all" else types.split(","))
    ph, cut = ",".join("?" * len(ids)), before or now() + 1
    aid = (s.get_agent(agent) or {}).get("id", "__none__") if agent else None
    ev: list[dict] = []
    def who(a):
        return s.label(a)[1] if a else None
    def add(rows_, typ, ts, title, text="", agent_=None, ref=None):
        for r in rows_:
            if r[ts] is None:
                continue
            ev.append({"t": r[ts], "at": iso(r[ts]), "type": typ(r) if callable(typ) else typ, "agent": who(agent_(r)) if agent_ else None,
                       "title": (title(r) or "")[:200], "text": (text(r) or "")[:280] if text else "", "ref": ref(r) if ref else None})
    by = (" AND agent_id=?", [aid]) if aid else ("", [])
    kinds = [k for k in ("decision", "handoff", "memory", "fact", "note", "observation", "document") if k in want]
    if kinds:
        add(s.q(f"SELECT id, kind, title, summary, content, agent_id, created_at FROM items WHERE project_id IN ({ph}) AND "
                f"kind IN ({','.join('?' * len(kinds))}) AND created_at<?{by[0]} ORDER BY created_at DESC LIMIT ?",
                ids + kinds + [cut] + by[1] + [limit]),
            lambda r: r["kind"], "created_at", lambda r: r["title"] or (r["summary"] or r["content"] or "")[:200],
            lambda r: (r["summary"] or r["content"] or "") if r["title"] else "", lambda r: r["agent_id"],
            lambda r: {"kind": "item", "id": r["id"]})
    if "session" in want:
        add(s.q(f"SELECT id, goal, status, agent_id, created_at FROM sessions WHERE project_id IN ({ph}) AND created_at<?{by[0]} "
                "ORDER BY created_at DESC LIMIT ?", ids + [cut] + by[1] + [limit]),
            "session", "created_at", lambda r: r["goal"] or "session", lambda r: r["status"], lambda r: r["agent_id"],
            lambda r: {"kind": "session", "id": r["id"]})
    if want & {"job", "job_done"} and not aid:
        jr = s.q(f"SELECT id, title, status, submitted_by, created_at, updated_at FROM jobs WHERE project_id IN ({ph}) AND "
                 "created_at<? ORDER BY created_at DESC LIMIT ?", ids + [cut, limit])
        if "job" in want:
            add(jr, "job", "created_at", lambda r: r["title"], lambda r: "submitted", lambda r: r["submitted_by"],
                lambda r: {"kind": "job", "id": r["id"]})
        if "job_done" in want:
            add([r for r in jr if r["status"] in ("done", "failed", "cancelled") and r["updated_at"] < cut], "job_done",
                "updated_at", lambda r: r["title"], lambda r: r["status"], None, lambda r: {"kind": "job", "id": r["id"]})
    if "step" in want:
        sb = (" AND t.claimed_by=?", [aid]) if aid else ("", [])
        add(s.q(f"SELECT t.id, t.job_id, t.title, t.status, t.result, t.claimed_by, t.updated_at FROM subtasks t JOIN jobs j "
                f"ON j.id=t.job_id WHERE j.project_id IN ({ph}) AND t.status IN ('done','failed') AND t.updated_at<?{sb[0]} "
                "ORDER BY t.updated_at DESC LIMIT ?", ids + [cut] + sb[1] + [limit]),
            "step", "updated_at", lambda r: r["title"], lambda r: r["result"] or r["status"], lambda r: r["claimed_by"],
            lambda r: {"kind": "job", "id": r["job_id"]})
    if want & {"work", "work_done"}:
        wr = s.q(f"SELECT id, what, status, result, agent_id, started_at, done_at FROM work_claims WHERE project_id IN ({ph}) AND "
                 f"started_at<?{by[0]} ORDER BY started_at DESC LIMIT ?", ids + [cut] + by[1] + [limit])
        if "work" in want:
            add(wr, "work", "started_at", lambda r: r["what"], None, lambda r: r["agent_id"], lambda r: {"kind": "claim", "id": r["id"]})
        if "work_done" in want:
            add([r for r in wr if r["done_at"] and r["done_at"] < cut], "work_done", "done_at", lambda r: r["what"],
                lambda r: r["result"] or r["status"], lambda r: r["agent_id"], lambda r: {"kind": "claim", "id": r["id"]})
    if "ask" in want:
        add(s.q(f"SELECT id, kind, text, status, agent_id, created_at FROM board WHERE project_id IN ({ph}) AND created_at<?{by[0]} "
                "ORDER BY created_at DESC LIMIT ?", ids + [cut] + by[1] + [limit]),
            "ask", "created_at", lambda r: r["text"], lambda r: f"{r['kind']} · {r['status']}", lambda r: r["agent_id"],
            lambda r: {"kind": "board", "id": r["id"]})
    if "change" in want:
        for c in changes(s, names, ids, limit=limit, diff_chars=0):
            if (c.get("ts") or 0) < cut and (not agent or c.get("by") == agent):
                ev.append({"t": c["ts"], "at": iso(c["ts"]), "type": "change", "agent": c.get("by"),
                           "title": (c.get("commit") or "")[:12] + (f" on {c['branch']}" if c.get("branch") else "") or "changes",
                           "text": ", ".join((c.get("files") or [])[:6]) if isinstance(c.get("files"), list) else "",
                           "ref": {"kind": "change", "path": c.get("path")}})
    if "file" in want:
        add(s.q(f"SELECT id, path, updated_by, updated_at FROM drive_files WHERE project IN ({','.join('?' * len(names))}) AND "
                "deleted_at IS NULL AND updated_at<? AND path NOT LIKE '%/changes.%' ORDER BY updated_at DESC LIMIT ?",
                names + [cut, limit]),
            "file", "updated_at", lambda r: r["path"], None, lambda r: r["updated_by"], lambda r: {"kind": "file", "path": r["path"]})
    ev.sort(key=lambda e: -e["t"])
    ev = ev[:limit]
    return {"events": [{k: v for k, v in e.items() if k != "t"} for e in ev],
            "next": ev[-1]["t"] if len(ev) == limit else None}


def group(s: Store, into: str, ids: list[str], title: str | None = None) -> dict:
    """Fold raw names (and anything already under them) into one project."""
    rows = _rows(s)
    root = _root(into, rows)
    with s.tx():
        for i in ids:
            i = i if i in rows else next((k for k, r in rows.items() if r["name"] == i), "")
            if not i:
                raise CommonsError("unknown project")
            if i == root:
                continue
            s.x("UPDATE projects SET parent_id=? WHERE id=? OR parent_id=?", (root, i, i))
            s.x("UPDATE projects SET kind=NULL WHERE id=? AND kind='folder'", (i,))
        if title is not None:
            s.x("UPDATE projects SET title=? WHERE id=?", ((title or "").strip()[:80] or None, root))
    s.audit(None, "project.group", root, {"ids": ids, "title": title})
    return detail(s, root)


def ungroup(s: Store, pid: str) -> dict:
    rows = _rows(s)
    if pid not in rows:
        raise CommonsError("unknown project")
    s.x("UPDATE projects SET parent_id=NULL WHERE id=?", (pid,))
    s.audit(None, "project.ungroup", pid, None)
    return {"ok": True, "id": pid}


def update(s: Store, pid: str, title: str | None = None, kind: str | None = None) -> dict:
    rows = _rows(s)
    root = _root(pid, rows)
    if kind is not None and kind not in KINDS + ("",):
        raise CommonsError(f"kind is one of {', '.join(KINDS)} (or empty for automatic)")
    if title is not None:
        s.x("UPDATE projects SET title=? WHERE id=?", (title.strip()[:80] or None, root))
    if kind is not None:
        s.x("UPDATE projects SET kind=? WHERE id=?", (kind or None, root))
    s.audit(None, "project.update", root, {"title": title, "kind": kind})
    return {"ok": True, "id": root}


def dismiss(s: Store, key: str) -> dict:
    if not re.fullmatch(r"g:[0-9a-f]{12}", key or ""):
        raise CommonsError("unknown suggestion")
    s.x("INSERT OR REPLACE INTO meta VALUES(?, ?)", (f"projsugg:{key}", json.dumps({"at": now()})))
    return {"ok": True}
