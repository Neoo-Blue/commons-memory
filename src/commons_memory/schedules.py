"""Every agent's scheduled tasks in one place, and takeover when the owner can't deliver.

An agent registers what it runs on a schedule (`schedule_report`: cron + timezone, how it works, the acceptance bar)
and reports each run (`schedule_ran`); the last good output is kept as the quality reference. The watcher hands a run
to another agent when the owner missed it (past due + grace with no ok report) or is out of quota until after the
grace window. The takeover job carries the instructions, the acceptance bar and the last good output, runs as
plan -> execute -> verify on agents other than the owner, and the verify step checks every acceptance point.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .ids import iso, new_id, now
from .store import CommonsError, Store

COLUMNS = [("recurring", "owner_agent", "TEXT"), ("recurring", "instructions", "TEXT"), ("recurring", "acceptance", "TEXT"),
           ("recurring", "how", "TEXT"), ("recurring", "external", "INTEGER DEFAULT 0"), ("recurring", "grace_s", "REAL"),
           ("recurring", "tz", "TEXT"), ("recurring", "last_ok", "REAL"), ("recurring", "last_status", "TEXT"),
           ("recurring", "quality_ref", "TEXT")]


def migrate(db) -> None:
    from .facts import add_columns
    add_columns(db, COLUMNS)


def _zone(tz: str | None):
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(tz or "UTC")
    except Exception as e:  # noqa: BLE001
        raise CommonsError(f"unknown timezone {tz!r} (use an IANA name like America/Los_Angeles)") from e


def last_slot(cron: str, tz: str | None, t: float, lookback_days: float = 8) -> float | None:
    """The most recent time <= t the cron fires, in the schedule's own timezone."""
    from .orchestrator import next_cron
    z = _zone(tz)
    cur = datetime.fromtimestamp(t - lookback_days * 86400, z).replace(tzinfo=None)
    end = datetime.fromtimestamp(t, z).replace(tzinfo=None)
    last = None
    for _ in range(20000):
        nxt = next_cron(cron, cur)
        if nxt > end:
            break
        last, cur = nxt, nxt
    return last.replace(tzinfo=z).timestamp() if last else None


def next_slot(cron: str, tz: str | None, t: float) -> float:
    from .orchestrator import next_cron
    z = _zone(tz)
    return next_cron(cron, datetime.fromtimestamp(t, z).replace(tzinfo=None)).replace(tzinfo=z).timestamp()


def register(s: Store, owner_id: str, name: str, cron: str, instructions: str, *, acceptance: str = "", how: str = "",
             project: str | None = None, tz: str = "UTC", grace_minutes: float = 30, external: bool = True) -> dict:
    if not (name or "").strip() or not (instructions or "").strip():
        raise CommonsError("schedule_report needs a name and the instructions another agent would follow")
    from .redact import redact
    nxt = next_slot(cron, tz, now())  # validates cron and timezone
    row = s.q("SELECT id, owner_agent FROM recurring WHERE name=?", (name,))
    if row and row[0]["owner_agent"] and row[0]["owner_agent"] != owner_id:
        raise CommonsError(f"schedule {name!r} belongs to another agent; pick another name")
    rid = row[0]["id"] if row else new_id("rec")
    vals = (redact(instructions)[0], "plan_execute_verify", s.project_id(project) if project else None, owner_id, cron, nxt,
            owner_id, redact(instructions)[0], redact(acceptance or "")[0], redact(how or "")[0], int(bool(external)),
            float(grace_minutes) * 60, tz)
    if row:
        s.x("UPDATE recurring SET task=?, pattern=?, project_id=?, created_by=?, cron=?, next_run=?, owner_agent=?, "
            "instructions=?, acceptance=?, how=?, external=?, grace_s=?, tz=?, enabled=1 WHERE id=?", (*vals, rid))
    else:
        s.x("INSERT INTO recurring(id,name,task,pattern,project_id,created_by,cron,next_run,owner_agent,instructions,"
            "acceptance,how,external,grace_s,tz,enabled,created_at,budget) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,'{}')",
            (rid, name, *vals, now()))
    s.audit(owner_id, "schedule.report", rid, {"name": name, "cron": cron, "tz": tz})
    return {"id": rid, "name": name, "next_run": iso(nxt), "external": bool(external)}


def ran(s: Store, agent_id: str, name: str, status: str = "ok", summary: str = "", output_ref: str = "") -> dict:
    r = s.q("SELECT * FROM recurring WHERE name=?", (name,))
    if not r:
        raise CommonsError(f"unknown schedule {name!r}; register it with schedule_report first")
    r = dict(r[0])
    if status not in ("ok", "failed", "partial"):
        raise CommonsError("status is ok, failed or partial")
    from .redact import redact
    t = now()
    sets = {"last_run": t, "last_status": f"{status} by {s.label(agent_id)[1]}"}
    if status == "ok":
        sets["last_ok"] = t
        if summary or output_ref:
            sets["quality_ref"] = redact(f"{summary}\n{output_ref}".strip())[0][:4000]
    s.x(f"UPDATE recurring SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), r["id"]))
    s.audit(agent_id, "schedule.ran", r["id"], {"status": status})
    return {"name": name, "status": status, "at": iso(t)}


def _brief(r: dict, owner: str, slot: float, reason: str) -> str:
    return (f"Takeover of the scheduled task '{r['name']}', normally run by {owner} on '{r['cron']}' ({r['tz'] or 'UTC'}). "
            f"The {iso(slot)} run was not delivered ({reason}). Do it now, to the same standard the owner meets.\n"
            f"FIRST check whether this run already happened (its output exists, the emails/posts went out). If it did, "
            f"call schedule_ran(name='{r['name']}', status='ok', summary='already done by the owner') and stop: never "
            f"repeat side effects.\n\n"
            f"HOW IT RUNS:\n{r['instructions']}\n" + (f"\n{r['how']}\n" if r.get("how") else "") +
            f"\nACCEPTANCE (the verify step checks every point):\n{r.get('acceptance') or '- the same output the owner produces'}\n"
            + (f"\nLAST GOOD OUTPUT (the quality reference):\n{r['quality_ref']}\n" if r.get("quality_ref") else "")
            + f"\nWhen done, call schedule_ran(name='{r['name']}', status='ok', summary=...) so the schedule shows it delivered.")


def watch(s: Store, t: float | None = None) -> list[dict]:
    """Start a takeover for every external schedule whose current run is missed or that its owner can't deliver."""
    from . import quota as Q
    from .orchestrator import Orchestrator
    t = t or now()
    out = []
    for r in s.q("SELECT * FROM recurring WHERE enabled=1 AND COALESCE(external,0)=1"):
        r = dict(r)
        slot = last_slot(r["cron"], r["tz"], t)
        if not slot or (r["last_ok"] or 0) >= slot or (r["created_at"] or 0) > slot:
            continue
        key = f"sched_cover:{r['id']}:{int(slot)}"
        cov = s.q("SELECT value FROM meta WHERE key=?", (key,))
        if cov:  # a takeover is running or done: close the loop when its job finished
            job = s.q("SELECT status FROM jobs WHERE id=?", (cov[0]["value"],))
            if job and job[0]["status"] == "done":
                s.x("UPDATE recurring SET last_ok=?, last_status=? WHERE id=?", (t, f"covered (job {cov[0]['value']})", r["id"]))
            continue
        pre = f"sched_cover:{r['id']}:"
        earlier = [m["value"] for m in s.q("SELECT value FROM meta WHERE substr(key, 1, ?)=?", (len(pre), pre))]
        jobs = s.q(f"SELECT id, status, updated_at FROM jobs WHERE id IN ({','.join('?' * len(earlier))})", earlier) if earlier else []
        if any(j["status"] not in ("done", "failed", "cancelled") for j in jobs):
            continue  # an earlier missed run's takeover is still open: it covers this run too (one per schedule, not per hour)
        late = [j for j in jobs if j["status"] == "done" and (j["updated_at"] or 0) >= slot]
        if late:  # it finished after this run was due, so it did this run's work
            s.x("UPDATE recurring SET last_ok=?, last_status=? WHERE id=?", (t, f"covered (job {late[0]['id']})", r["id"]))
            continue
        grace = r["grace_s"] or 1800
        owner = r["owner_agent"]
        ok, until, win = Q.can_take(s, owner) if owner else (True, None, None)
        if t >= slot + grace and r["last_ok"]:  # an owner that never reported a run can't be told apart from a missed one
            reason = "no ok report within the grace window"
        elif not ok and until and until > slot + grace:
            reason = f"the owner is out of quota ({win}) until {iso(until)}"
        else:
            continue
        o = Orchestrator(s)
        job = o.submit(_brief(r, s.label(owner)[1] if owner else "-", slot, reason), pattern="plan_execute_verify",
                       project=s.project_name(r["project_id"]) if r["project_id"] else None,
                       title=f"{r['name']} @ {iso(slot)} (takeover)", recurring_id=r["id"])
        if owner:  # someone other than the owner, who can't deliver right now
            for st in s.q("SELECT id, excluded_agents FROM subtasks WHERE job_id=?", (job["job_id"],)):
                ex = sorted(set(json.loads(st["excluded_agents"] or "[]")) | {owner})
                s.x("UPDATE subtasks SET excluded_agents=?, assigned_agent=NULL WHERE id=? AND status='pending'",
                    (json.dumps(ex), st["id"]))
            o.allocate(job["job_id"])
        s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, job["job_id"]))
        s.audit(None, "schedule.takeover", r["id"], {"slot": iso(slot), "job": job["job_id"], "reason": reason})
        try:
            from .escalation import notify
            notify(s, f"Took over '{r['name']}' ({iso(slot)}): {reason}")
        except Exception:  # noqa: BLE001
            pass
        out.append({"schedule": r["name"], "slot": iso(slot), "job_id": job["job_id"], "reason": reason})
    return out


def listing(s: Store, agent: str | None = None) -> list[dict]:
    where, args = "WHERE COALESCE(external,0)=1 OR owner_agent IS NOT NULL", []
    if agent:
        a = s.get_agent(agent)
        where += " AND owner_agent=?"
        args.append(a["id"] if a else agent)
    out, t = [], now()
    for r in s.q(f"SELECT * FROM recurring {where} ORDER BY name", args):
        r = dict(r)
        slot = last_slot(r["cron"], r["tz"], t)
        out.append({"name": r["name"], "owner": s.label(r["owner_agent"])[1] if r["owner_agent"] else None,
                    "cron": r["cron"], "tz": r["tz"] or "UTC", "external": bool(r["external"]),
                    "next_run": iso(next_slot(r["cron"], r["tz"], t)), "last_slot": iso(slot) if slot else None,
                    "delivered": bool(slot and (r["last_ok"] or 0) >= slot), "last_ok": iso(r["last_ok"]) if r["last_ok"] else None,
                    "last_status": r["last_status"], "acceptance": (r["acceptance"] or "")[:200], "how": (r["how"] or "")[:200]})
    return out
