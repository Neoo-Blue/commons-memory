"""Who is doing what, so no two agents do the same work and nobody redoes finished work.

start_work(what, project, paths) before any task: Commons compares it with every other agent's live work (open
claims, claimed subtasks) and with work finished in the last 30 days (finished claims, closed board items, handoffs).
Live overlap = don't start: coordinate with the owner or tell the user. Finished overlap = build on it, or start anyway
with force and the reason (a real follow-up). finish_work, or handoff, closes the claim with its result, which is what
the next agent is shown. The prompt hooks and turn() run the same check on every user message, so a hooked agent is
warned even when it forgets to ask.
"""
from __future__ import annotations

import json
import os
import re

from .ids import iso, new_id, now
from .store import CommonsError, Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_claims(id TEXT PRIMARY KEY, agent_id TEXT, project_id TEXT, what TEXT, paths TEXT,
  status TEXT, reason TEXT, result TEXT, vec BLOB, started_at REAL, done_at REAL);
CREATE INDEX IF NOT EXISTS work_claims_st ON work_claims(status, started_at);
"""
STOP = set("the and for with that this from into onto have has was were are you your our their its not but all any "
           "can will should would could make made add fix new use using also just then than them they what when "
           "which who how why please".split())


def migrate(db) -> None:
    db.executescript(SCHEMA)
    from .facts import add_columns
    add_columns(db, [("work_claims", "related", "TEXT")])


def related(s: Store, what: str, project: str | None = None, paths: list[str] | None = None, limit: int = 8) -> list[dict]:
    """Memories, decisions, documents and handoffs connected to this work (by meaning, or naming one of its paths):
    when the work changes something, these are the portions that may need updating too. Conversation records are
    history, not facts, so they're left out."""
    hits: list[str] = []
    for p in paths or []:  # anything that names a file or place this work touches
        base = os.path.basename(p.rstrip("/")) or p
        if len(base) >= 4:
            hits += [r["id"] for r in s.q("SELECT id FROM items WHERE content LIKE ? AND superseded_by IS NULL "
                                         "AND kind<>'observation' ORDER BY created_at DESC LIMIT 5", (f"%{base}%",))]
    try:
        ids, _, why = s.rank(f"{what} {' '.join(paths or [])}", project=project, limit=limit * 3,
                             kinds=["memory", "fact", "decision", "document", "handoff"])
    except Exception:  # noqa: BLE001
        ids, why = [], {}
    out, seen = [], set()
    for i in hits + [i for i in ids or [] if why.get(i, {}).get("sim", 0) >= float(os.environ.get("COMMONS_RELATED_SIM", "0.6"))]:
        if i in seen:
            continue
        seen.add(i)
        r = s.q("SELECT id, kind, summary, tags FROM items WHERE id=? AND superseded_by IS NULL", (i,))
        if r and '"conversation"' not in (r[0]["tags"] or ""):
            out.append({"id": i, "kind": r[0]["kind"], "summary": (r[0]["summary"] or "")[:160]})
        if len(out) >= limit:
            break
    return out


def _words(t: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", (t or "").lower())) - STOP


def _vec(s: Store, text: str):
    if not s.embedder:
        return None
    try:
        return s.embedder.embed([text[:2000]])[0]
    except Exception:  # noqa: BLE001 - matching falls back to words
        return None


def _similar(s: Store, a: str, av, b: str, bv=None) -> float:
    """0-1: the larger of embedding cosine and word overlap (shared words / the shorter text's words, >= 2 shared)."""
    wa, wb = _words(a), _words(b)
    shared = len(wa & wb)
    score = shared / min(len(wa), len(wb)) if shared >= 2 else 0.0
    if av is not None:
        from .embed import cosine, unpack
        bv = unpack(bv) if isinstance(bv, (bytes, bytearray, memoryview)) else (bv if bv is not None else _vec(s, b))
        if bv is not None:
            score = max(score, cosine(av, bv))
    return score


def _threshold() -> float:
    return float(os.environ.get("COMMONS_DUP_SIM", "0.8"))


def overlaps(s: Store, agent_id: str | None, what: str, project: str | None = None, paths: list[str] | None = None,
             days: float = 30) -> dict:
    """{"busy": live work by other agents that overlaps, "done": finished work that overlaps}, best match first."""
    t, th, paths = now(), _threshold(), set(paths or [])
    pid = (s.q("SELECT id FROM projects WHERE name=? OR id=?", (project, project)) or [{"id": None}])[0]["id"] if project else None
    av = _vec(s, what)
    stale = float(os.environ.get("COMMONS_WORK_STALE", "7200"))
    busy, done = [], []

    def same_project(p):
        return pid is None or p is None or p == pid

    for r in s.q("SELECT c.*, a.name, a.last_seen FROM work_claims c JOIN agents a ON a.id=c.agent_id "
                 "WHERE c.status='active' AND c.agent_id<>? ORDER BY c.started_at DESC LIMIT 50", (agent_id or "",)):
        if t - (r["last_seen"] or r["started_at"] or 0) > stale or not same_project(r["project_id"]):
            continue  # an agent silent for hours isn't working on it any more
        shared = sorted(paths & set(json.loads(r["paths"] or "[]")))
        sim = 1.0 if shared else _similar(s, what, av, r["what"], r["vec"])
        if sim >= th:
            busy.append({"claim_id": r["id"], "agent": r["name"], "what": r["what"], "since": iso(r["started_at"]),
                         "paths": shared, "match": round(sim, 2)})
    for r in s.q("SELECT st.id, st.title, st.job_id, a.name FROM subtasks st JOIN agents a ON a.id=st.claimed_by "
                 "WHERE st.status='claimed' AND st.claimed_by<>? LIMIT 100", (agent_id or "",)):
        sim = _similar(s, what, av, r["title"] or "")
        if sim >= th:
            busy.append({"subtask_id": r["id"], "agent": r["name"], "what": r["title"], "match": round(sim, 2)})
    since = t - days * 86400
    for r in s.q("SELECT c.*, a.name FROM work_claims c JOIN agents a ON a.id=c.agent_id WHERE c.status='done' "
                 "AND c.done_at>? ORDER BY c.done_at DESC LIMIT 100", (since,)):
        if same_project(r["project_id"]):
            sim = _similar(s, what, av, r["what"], r["vec"])
            if sim >= th:
                done.append({"claim_id": r["id"], "agent": r["name"], "what": r["what"], "when": iso(r["done_at"]),
                             "result": (r["result"] or "")[:400], "match": round(sim, 2)})
    for r in s.q("SELECT id, kind, text, evidence, source, closed_at, project_id FROM board WHERE status='done' "
                 "AND kind IN ('ask','delivered','action') AND closed_at>? ORDER BY closed_at DESC LIMIT 300", (since,)):
        if same_project(r["project_id"]):
            sim = _similar(s, what, None, r["text"])  # words only: hundreds of rows, no per-row embedding
            if sim >= th:
                done.append({"board_id": r["id"], "agent": r["source"], "what": r["text"][:200], "when": iso(r["closed_at"]),
                             "result": (r["evidence"] or "")[:400], "match": round(sim, 2)})
    try:  # handoffs carry stored vectors: rank them instead of embedding each
        ids, _, why = s.rank(what, project=project, kinds=["handoff"], since=f"{int(days)}d", limit=3)
        for i in ids or []:
            if why.get(i, {}).get("sim", 0) >= th:
                h = s.q("SELECT i.summary, i.created_at, a.name FROM items i LEFT JOIN agents a ON a.id=i.agent_id "
                        "WHERE i.id=?", (i,))[0]
                done.append({"handoff_id": i, "agent": h["name"], "what": h["summary"][:200], "when": iso(h["created_at"]),
                             "match": round(why[i]["sim"], 2)})
    except Exception:  # noqa: BLE001
        pass
    key = lambda x: -x["match"]  # noqa: E731
    return {"busy": sorted(busy, key=key)[:5], "done": sorted(done, key=key)[:5]}


def start(s: Store, agent_id: str, what: str, project: str | None = None, paths: list[str] | None = None,
          force: bool = False, reason: str = "") -> dict:
    if not (what or "").strip():
        raise CommonsError("start_work needs `what`: the task in one line")
    ov = overlaps(s, agent_id, what, project, paths)
    if ov["busy"]:
        return {"ok": False, "status": "busy", **ov, "do": "Another agent is on this now. Don't start it: msg_send them to coordinate (split "
                                         "it, or wait for their finish_work), or tell the user who has it."}
    if ov["done"] and not force:
        return {"ok": False, "status": "done", **ov, "do": "This was done before. Read it (expand/resume the ids) and build on it, or tell "
                                         "the user it's done. A genuine follow-up: call again with force=true and the "
                                         "reason."}
    if force and ov["done"] and not (reason or "").strip():
        raise CommonsError("force needs a reason: what this adds to the earlier work")
    from .embed import pack
    from .redact import redact
    v = _vec(s, what)
    cid = new_id("wrk")
    s.x("INSERT INTO work_claims(id,agent_id,project_id,what,paths,status,reason,vec,started_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (cid, agent_id, s.project_id(project) if project else None, redact(what)[0][:1000], json.dumps(sorted(paths or [])),
         "active", reason or None, pack(v) if v is not None else None, now()))
    from .status import put as status_put
    status_put(s, agent_id, "working", what)
    rel = related(s, what, project, paths)
    s.x("UPDATE work_claims SET related=? WHERE id=?", (json.dumps([r["id"] for r in rel]), cid))
    s.audit(agent_id, "work.start", cid, {"what": what[:200], "forced": bool(force)})
    try:  # a reusable how-to for this kind of work, from the shared skill pool
        from .skillpool import SkillPool
        skills = [{"name": x.get("name"), "description": (x.get("description") or "")[:120]}
                  for x in SkillPool(s).suggest(what, limit=3)]
    except Exception:  # noqa: BLE001
        skills = []
    return {"ok": True, "status": "ok", "claim_id": cid, "builds_on": ov["done"], "related": rel, "skills": skills,
            "do": "It's yours: others are warned off. `related` is what's connected to this work: if you change it, "
                  "update those too. `skills` are pool how-tos for this (skill_get). Call finish_work(claim_id, "
                  "result) (or handoff) when done."}


def finish(s: Store, agent_id: str, claim_id: str = "", result: str = "", status: str = "done",
           changes: dict | None = None) -> dict:
    if status not in ("done", "dropped"):
        raise CommonsError("status is done or dropped")
    from .redact import redact
    where, args = ("id=? AND agent_id=?", (claim_id, agent_id)) if claim_id else ("agent_id=? AND status='active'", (agent_id,))
    ids = [r["id"] for r in s.q(f"SELECT id FROM work_claims WHERE {where}", args)]
    if claim_id and not ids:
        raise CommonsError(f"{claim_id} is not your work claim")
    check = []
    for i in ids:
        s.x("UPDATE work_claims SET status=?, result=?, done_at=? WHERE id=?", (status, redact(result or "")[0][:4000], now(), i))
        row = s.q("SELECT related FROM work_claims WHERE id=?", (i,))[0]
        for rid in json.loads(row["related"] or "[]"):
            r = s.q("SELECT id, kind, summary FROM items WHERE id=? AND superseded_by IS NULL", (rid,))
            if r and rid not in [c["id"] for c in check]:
                check.append({"id": rid, "kind": r[0]["kind"], "summary": (r[0]["summary"] or "")[:160]})
                s.relate(i, rid, "may_affect", agent_id=agent_id)
    if ids:
        from .status import put as status_put
        status_put(s, agent_id, "done", (result or "finished").split("\n")[0])
    out = {"closed": ids, "status": status}
    if changes and ids:
        from . import workspace as WS
        try:
            out["changes"] = WS.record_changes(s, agent_id, claim=ids[0], source="agent",
                                               **{k: changes.get(k) or "" for k in ("repo", "branch", "commit", "diff")})
        except Exception as e:  # noqa: BLE001 - the claim is closed either way
            out["changes"] = {"error": str(e)[:200]}
    if check and status == "done":
        out |= {"check_related": check,
                "do": "Your change may have made these outdated. Update each one it affects (remember with related_ids, "
                      "a superseding fact, or edit the doc), or note why it still holds."}
    return out


def active(s: Store, project: str | None = None) -> list[dict]:
    rows = s.q("SELECT c.id, c.what, c.paths, c.started_at, c.project_id, a.name FROM work_claims c JOIN agents a "
               "ON a.id=c.agent_id WHERE c.status='active' ORDER BY c.started_at DESC LIMIT 100")
    pid = (s.q("SELECT id FROM projects WHERE name=? OR id=?", (project, project)) or [{"id": None}])[0]["id"] if project else None
    return [{"claim_id": r["id"], "agent": r["name"], "what": r["what"], "paths": json.loads(r["paths"] or "[]"),
             "since": iso(r["started_at"]), "project": s.project_name(r["project_id"]) if r["project_id"] else None}
            for r in rows if not pid or r["project_id"] in (None, pid)]


def warning(s: Store, agent_id: str | None, text: str, project: str | None) -> str:
    """For the prompt hooks: what other agents are doing, or already did, that matches the user's message."""
    if len(_words(text)) < 3:
        return ""
    ov = overlaps(s, agent_id, text, project)
    out = [f"BUSY: {b['agent']} is working on this now: {b['what'][:140]}" for b in ov["busy"]]
    out += [f"DONE BEFORE: {d['agent'] or '?'} ({(d.get('when') or '')[:10]}): {d['what'][:140]}"
            + (f" -> {d['result'][:160]}" if d.get("result") else "") for d in ov["done"][:3]]
    if not out:
        return ""
    return ("Commons work check (never duplicate another agent's work; build on finished work):\n"
            + "\n".join(out) + "\n\n")
