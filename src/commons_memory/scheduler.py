"""Concurrent multi-project scheduling.

Many projects and jobs run at once. Claims are ordered by an effective priority:
    job.priority*10 + project.priority*5 + subtask.priority - fair_share
where fair_share = (subtasks the project currently has in flight) / project.weight, so a busy project yields to idle ones.
Limits: per-project max concurrent in-flight subtasks and $ budget, per-agent capacity (profile max_concurrent),
paused/preempted projects and jobs get no new claims. Jobs can wait for other jobs, including jobs in other projects
(`after`). Preemption pauses lower-priority jobs (optionally requeueing their in-flight subtasks).
Isolation: context, Drive and ACLs are per project (existing mechanisms); claims never mix project contexts.
"""
from __future__ import annotations

import json

from .ids import now
from .store import CommonsError, Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS project_sched(project_id TEXT PRIMARY KEY, priority INTEGER DEFAULT 0, weight REAL DEFAULT 1,
  max_concurrent INTEGER, budget_cost REAL, paused INTEGER DEFAULT 0, updated_at REAL);
"""
COLUMNS = [("jobs", "priority", "INTEGER DEFAULT 0"), ("jobs", "after_jobs", "TEXT")]


def add_columns(db):
    for t, c, typ in COLUMNS:
        cols = {r[1] for r in db.execute(f"PRAGMA table_info({t})")}
        if cols and c not in cols:
            db.execute(f"ALTER TABLE {t} ADD COLUMN {c} {typ}")


class Scheduler:
    def __init__(self, s: Store):
        self.s = s

    # ---- config
    def configure(self, project: str, *, priority: int | None = None, weight: float | None = None,
                  max_concurrent: int | None = None, budget_cost: float | None = None, paused: bool | None = None) -> dict:
        pid = self.s.project_id(project)
        cur = self.s.q("SELECT * FROM project_sched WHERE project_id=?", (pid,))
        row = dict(cur[0]) if cur else {"priority": 0, "weight": 1.0, "max_concurrent": None, "budget_cost": None, "paused": 0}
        for k, v in (("priority", priority), ("weight", weight), ("max_concurrent", max_concurrent),
                     ("budget_cost", budget_cost), ("paused", None if paused is None else int(paused))):
            if v is not None:
                row[k] = v
        if row["weight"] is not None and float(row["weight"]) <= 0:
            raise CommonsError("weight must be > 0")
        self.s.x("INSERT OR REPLACE INTO project_sched(project_id,priority,weight,max_concurrent,budget_cost,paused,updated_at) "
                 "VALUES(?,?,?,?,?,?,?)", (pid, row["priority"], row["weight"], row["max_concurrent"], row["budget_cost"],
                                          row["paused"], now()))
        return self.project(pid)

    def project(self, pid: str | None) -> dict:
        r = self.s.q("SELECT * FROM project_sched WHERE project_id=?", (pid,)) if pid else []
        base = dict(r[0]) if r else {"project_id": pid, "priority": 0, "weight": 1.0, "max_concurrent": None,
                                     "budget_cost": None, "paused": 0}
        return base | {"project": self.s.project_name(pid) if pid else None}

    def set_job(self, job_id: str, *, priority: int | None = None, after: list[str] | None = None) -> dict:
        if priority is not None:
            self.s.x("UPDATE jobs SET priority=? WHERE id=?", (priority, job_id))
        if after is not None:
            for a in after:
                if not self.s.q("SELECT 1 FROM jobs WHERE id=?", (a,)):
                    raise CommonsError(f"unknown job {a}")
            if job_id in after or self._reaches(after, job_id):
                raise CommonsError("job dependency cycle")
            self.s.x("UPDATE jobs SET after_jobs=? WHERE id=?", (json.dumps(after), job_id))
        r = self.s.q("SELECT id, priority, after_jobs, status FROM jobs WHERE id=?", (job_id,))[0]
        return dict(r)

    def _reaches(self, starts: list[str], target: str) -> bool:
        seen, stack = set(), list(starts)
        while stack:
            j = stack.pop()
            if j == target:
                return True
            if j in seen:
                continue
            seen.add(j)
            r = self.s.q("SELECT after_jobs FROM jobs WHERE id=?", (j,))
            stack += json.loads(r[0]["after_jobs"] or "[]") if r else []
        return False

    # ---- preemption
    def preempt(self, below: int, requeue: bool = False, reason: str = "preempted for higher-priority work") -> dict:
        jobs = [r["id"] for r in self.s.q("SELECT id FROM jobs WHERE status='running' AND COALESCE(priority,0)<?", (below,))]
        rq = 0
        for j in jobs:
            self.s.x("UPDATE jobs SET status='preempted', updated_at=? WHERE id=?", (now(), j))
            if requeue:
                rq += len(self.s.q("SELECT id FROM subtasks WHERE job_id=? AND status='claimed'", (j,)))
                self.s.x("UPDATE subtasks SET status='pending', claimed_by=NULL, lease_expires=NULL, attempts=MAX(attempts-1,0) "
                         "WHERE job_id=? AND status='claimed'", (j,))
            self.s.audit(None, "job.preempt", j, reason)
        return {"preempted": jobs, "requeued": rq}

    def resume(self, job_id: str | None = None) -> dict:
        sql = "UPDATE jobs SET status='running', updated_at=? WHERE status='preempted'" + (" AND id=?" if job_id else "")
        n = self.s.q("SELECT COUNT(*) n FROM jobs WHERE status='preempted'" + (" AND id=?" if job_id else ""),
                     (job_id,) if job_id else ())[0]["n"]
        self.s.x(sql, (now(), job_id) if job_id else (now(),))
        return {"resumed": n}

    # ---- claim ordering
    def inflight(self) -> dict[str | None, int]:
        return {r["project_id"]: r["n"] for r in self.s.q(
            "SELECT j.project_id, COUNT(*) n FROM subtasks s JOIN jobs j ON j.id=s.job_id WHERE s.status='claimed' "
            "GROUP BY j.project_id")}

    def project_cost(self, pid: str | None) -> float:
        tot = 0.0
        for r in self.s.q("SELECT used FROM jobs WHERE project_id IS ?", (pid,)):
            tot += float(json.loads(r["used"] or "{}").get("cost", 0))
        return tot

    def order(self, rows: list[dict]) -> list[dict]:
        """Filter and sort claimable subtasks (rows carry job_id)."""
        if not rows:
            return rows
        inflight = self.inflight()
        jobs: dict[str, dict] = {}
        out = []
        for st in rows:
            j = jobs.get(st["job_id"])
            if j is None:
                j = jobs[st["job_id"]] = dict(self.s.q("SELECT id, project_id, priority, after_jobs FROM jobs WHERE id=?",
                                                       (st["job_id"],))[0])
                j["sched"] = self.project(j["project_id"])
                deps = json.loads(j["after_jobs"] or "[]")
                j["blocked"] = bool(deps) and bool(self.s.q(
                    f"SELECT 1 FROM jobs WHERE id IN ({','.join('?' * len(deps))}) AND status NOT IN ('done','cancelled')", deps))
                dead = deps and self.s.q(f"SELECT id, status FROM jobs WHERE id IN ({','.join('?' * len(deps))}) "
                                         "AND status IN ('failed','budget_exceeded')", deps)
                if dead:  # a prerequisite can never finish: fail this job instead of blocking it forever
                    self.s.x("UPDATE jobs SET status='failed', result=?, updated_at=? WHERE id=? AND status NOT IN "
                             "('failed','done','cancelled')", (f"prerequisite job {dead[0]['id']} {dead[0]['status']}",
                                                               now(), j["id"]))
                    self.s.x("UPDATE subtasks SET status='cancelled', updated_at=? WHERE job_id=? AND status IN "
                             "('pending','ready')", (now(), j["id"]))
                ps = j["sched"]
                j["full"] = bool(ps["max_concurrent"]) and inflight.get(j["project_id"], 0) >= int(ps["max_concurrent"])
                j["broke"] = bool(ps["budget_cost"]) and self.project_cost(j["project_id"]) >= float(ps["budget_cost"])
            ps = j["sched"]
            if ps["paused"] or j["blocked"] or j["full"] or j["broke"]:
                continue
            fair = inflight.get(j["project_id"], 0) / float(ps["weight"] or 1)
            st["_eff"] = (j["priority"] or 0) * 10 + (ps["priority"] or 0) * 5 + (st.get("priority") or 0) - fair
            out.append(st)
        out.sort(key=lambda x: (-x["_eff"], x["created_at"]))
        return out

    # ---- mission control
    def mission_control(self) -> dict:
        from .messaging import board
        t = now()
        projects = []
        inflight = self.inflight()
        for p in self.s.q("SELECT DISTINCT project_id FROM jobs WHERE status IN ('running','preempted')"):
            pid = p["project_id"]
            jobs = [dict(r) for r in self.s.q("SELECT id, title, status, priority, after_jobs, used FROM jobs WHERE project_id IS ? "
                                              "AND status IN ('running','preempted') ORDER BY priority DESC, created_at", (pid,))]
            for j in jobs:
                c = {r["status"]: r["n"] for r in self.s.q("SELECT status, COUNT(*) n FROM subtasks WHERE job_id=? GROUP BY status", (j["id"],))}
                j["subtasks"] = c
                j["progress"] = round(c.get("done", 0) / max(1, sum(c.values())), 2)
                j["cost"] = float(json.loads(j.pop("used") or "{}").get("cost", 0))
            done_h = self.s.q("SELECT COUNT(*) n FROM subtasks s JOIN jobs j ON j.id=s.job_id WHERE j.project_id IS ? AND "
                              "s.status='done' AND s.updated_at>?", (pid, t - 3600))[0]["n"]
            queued = self.s.q("SELECT COUNT(*) n FROM subtasks s JOIN jobs j ON j.id=s.job_id WHERE j.project_id IS ? AND "
                              "s.status='pending' AND j.status='running'", (pid,))[0]["n"]
            projects.append(self.project(pid) | {"jobs": jobs, "inflight": inflight.get(pid, 0), "queued": queued,
                                                 "throughput_per_h": done_h, "cost": round(self.project_cost(pid), 4)})
        agents = []
        from .orchestrator import Profiles
        prof = Profiles(self.s)
        for a in board(self.s):
            if a["agent"] in ("user",):
                continue
            row = self.s.q("SELECT profile FROM agents WHERE id=?", (a["id"],))
            cap = int((json.loads(row[0]["profile"] or "{}") if row else {}).get("max_concurrent", 2) or 2)
            agents.append(a | {"load": prof.load(a["id"]), "capacity": cap, "working_on": [
                {"subtask": r["id"], "title": r["title"], "job": r["job_id"]} for r in self.s.q(
                    "SELECT id, title, job_id FROM subtasks WHERE claimed_by=? AND status='claimed'", (a["id"],))]})
        return {"projects": sorted(projects, key=lambda p: (-(p["priority"] or 0), p["project"] or "")), "agents": agents,
                "totals": {"running_jobs": sum(len(p["jobs"]) for p in projects), "inflight": sum(inflight.values()),
                           "done_last_hour": sum(p["throughput_per_h"] for p in projects)}}
