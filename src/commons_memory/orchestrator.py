"""Distributed orchestration across agents from different platforms.

- Capability profiles (expertise per task type, strengths, cost, speed, availability, limits)
- Outcome learning (Beta-smoothed success rate per agent x task type) feeding the allocator
- Planner: decomposes a task (single or huge) into a DAG of typed subtasks using built-in collaboration
  patterns (plan_execute_verify, cross_review, debate, consensus, parallel) or an explicit/LLM-provided DAG
- Allocator: expertise + learned score - load - cost, respecting availability and author != reviewer
- Claims with leases + heartbeats; reaper retries/reassigns expired claims; dependency tracking
- Quality gates: a gated subtask is only done after all its gates (review / verify / red-team) approve
- Budgets (tokens / seconds / cost) per job; results merged into memory on completion
- Leftovers + recurring (cron) tasks that carry leftovers forward; pickup() for agents
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta

from .ids import estimate_tokens, iso, new_id, now
from .prefs import Preferences
from .redact import redact
from .store import CommonsError, Store

TASK_TYPES = ["research", "plan", "code", "review", "debate", "judge", "red_team", "test", "summarize", "vote",
              "tally", "general"]
GATE_TYPES = {"review", "red_team", "test", "verify"}
DEFAULT_LEASE = int(os.environ.get("COMMONS_LEASE_SECONDS", "900"))


def _j(v):
    return json.dumps(v, ensure_ascii=False, default=str)


# ============================================================================ profiles & learning
class Profiles:
    def __init__(self, store: Store):
        self.s = store

    def set(self, agent_id: str, *, expertise: dict[str, float] | None = None, strengths: list[str] | None = None,
            cost: float | None = None, speed: float | None = None, availability: str | None = None,
            limits: dict | None = None, max_concurrent: int | None = None, platform: str | None = None,
            notes: str | None = None, reach: list[str] | None = None, skills: list[str] | None = None) -> dict:
        a = self.s.get_agent(agent_id)
        if not a:
            raise CommonsError(f"unknown agent {agent_id}")
        prof = json.loads(a.get("profile") or "{}")
        upd = {"expertise": expertise, "strengths": strengths, "cost": cost, "speed": speed,
               "availability": availability, "limits": limits, "max_concurrent": max_concurrent, "notes": notes,
               "reach": reach, "skills": skills}
        for k, v in upd.items():
            if v is not None:
                prof[k] = ({**prof.get("expertise", {}), **v} if k == "expertise" else v)
        self.s.x("UPDATE agents SET profile=?, platform=COALESCE(?,platform) WHERE id=?",
                 (_j(prof), platform, a["id"]))
        return self.get(a["id"])

    def get(self, agent_id: str) -> dict:
        a = self.s.get_agent(agent_id)
        if not a:
            raise CommonsError(f"unknown agent {agent_id}")
        prof = json.loads(a.get("profile") or "{}")
        learned = {}
        for r in self.s.q("SELECT task_type, COUNT(*) n, SUM(success) s, AVG(rating) r FROM outcomes WHERE agent_id=? "
                          "GROUP BY task_type", (a["id"],)):
            learned[r["task_type"]] = {"n": r["n"], "success_rate": round((r["s"] + 1) / (r["n"] + 2), 3),
                                       "avg_rating": round(r["r"], 2) if r["r"] is not None else None}
        return {"id": a["id"], "name": a["name"], "platform": a.get("platform"), "vendor": a.get("vendor"),
                "model": a.get("model"), "profile": prof, "learned": learned, "load": self.load(a["id"])}

    def load(self, agent_id: str) -> int:
        return self.s.q("SELECT COUNT(*) c FROM subtasks WHERE claimed_by=? AND status='claimed'", (agent_id,))[0]["c"]

    def record_outcome(self, agent_id: str, task_type: str, success: bool, rating: float | None = None,
                       subtask_id: str | None = None, tokens: int | None = None, seconds: float | None = None,
                       note: str = "") -> dict:
        self.s.x("INSERT INTO outcomes(agent_id,task_type,success,rating,subtask_id,tokens,seconds,note,created_at)"
                 " VALUES(?,?,?,?,?,?,?,?,?)", (agent_id, task_type, int(bool(success)), rating, subtask_id, tokens,
                                                seconds, note, now()))
        if subtask_id:
            from .routing import Router
            oc = {"gate rejected": "rejected", "lease timeout": "timeout"}.get(note, "success" if success else "failed")
            Router(self.s).record_outcome(subtask_id, oc, rating)
        if subtask_id and note in ("gate rejected", "gates passed", "lease timeout"):
            # real task outcome -> learned model score (feeds routing + post-switch A/B validation)
            m = self.s.q("SELECT model FROM usage WHERE subtask_id=? AND model IS NOT NULL ORDER BY ts DESC LIMIT 1",
                         (subtask_id,))
            if m and m[0]["model"]:
                from .providers import ProviderLayer
                ProviderLayer(self.s).record_score(m[0]["model"], task_type, rating if rating is not None else float(bool(success)))
        return {"agent_id": agent_id, "task_type": task_type, "success": bool(success)}

    def score(self, agent: dict, task_type: str) -> float:
        prof = json.loads(agent.get("profile") or "{}")
        if prof.get("availability") in ("offline", "unavailable", "paused"):
            return -1e9
        exp = prof.get("expertise", {})
        e = float(exp.get(task_type, exp.get("general", 0.3)))
        r = self.s.q("SELECT COUNT(*) n, COALESCE(SUM(success),0) s FROM outcomes WHERE agent_id=? AND task_type=?",
                     (agent["id"], task_type))[0]
        learned = (r["s"] + 1) / (r["n"] + 2)            # Beta(1,1) prior
        weight = min(1.0, r["n"] / 5)                     # trust learned data as it accumulates
        quality = (1 - 0.5 * weight) * e + 0.5 * weight * learned + 0.25 * learned
        load = self.load(agent["id"])
        maxc = int(prof.get("max_concurrent", 2) or 2)
        if load >= maxc:
            quality -= 0.5
        quality -= 0.1 * load
        cost = float(prof.get("cost", 3) or 3)           # 1 (cheap) .. 5 (expensive)
        speed = float(prof.get("speed", 3) or 3)         # 1 (slow) .. 5 (fast)
        t = self.s.q("SELECT AVG(seconds) a, COUNT(seconds) n FROM outcomes WHERE agent_id=? AND task_type=? AND "
                     "success=1 AND seconds>0", (agent["id"], task_type))[0]
        if t["n"] >= 3:  # learned efficiency: faster than the team's average on this kind of work
            team = self.s.q("SELECT AVG(seconds) a FROM outcomes WHERE task_type=? AND success=1 AND seconds>0",
                            (task_type,))[0]["a"]
            quality += 0.1 * max(-1.0, min(1.0, (team - t["a"]) / team)) if team else 0
        return quality - 0.03 * cost + 0.02 * speed

    def rank(self, task_type: str, exclude: list[str] | None = None) -> list[tuple[float, dict]]:
        agents = [dict(r) for r in self.s.q("SELECT * FROM agents")]
        out = [(self.score(a, task_type), a) for a in agents if a["id"] not in (exclude or [])
               and a.get("profile")]  # only agents that declared a profile take orchestrated work
        return sorted([x for x in out if x[0] > -1e8], key=lambda x: -x[0])


# ============================================================================ planner
def _items(text: str) -> list[str]:
    items = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", ln).strip() for ln in text.splitlines()
             if re.match(r"^\s*(?:[-*•]|\d+[.)])\s+\S", ln)]
    return [i for i in items if i]


def plan(task: str, pattern: str = "auto", n: int = 3) -> list[dict]:
    """Return a DAG: list of {key, type, title, body, deps, gates}."""
    hook = os.environ.get("COMMONS_PLANNER_CMD")
    if hook and pattern == "llm":
        out = subprocess.run(hook, shell=True, input=task, capture_output=True, text=True, timeout=120).stdout
        return json.loads(out)
    items = _items(task)
    head = task.strip().splitlines()[0][:120] if task.strip() else "task"
    if pattern == "auto":
        low = task.lower()
        if re.search(r"\b(debate|pros and cons|trade-?offs?|should we|versus|vs\.?)\b", low):
            pattern = "debate"
        elif re.search(r"\b(vote|consensus|agree on)\b", low):
            pattern = "consensus"
        elif len(items) >= 2:
            pattern = "parallel"
        elif re.search(r"\b(implement|code|build|fix|refactor|write)\b", low):
            pattern = "cross_review"
        else:
            pattern = "plan_execute_verify"
    if pattern == "debate":
        dag = [{"key": f"debater{i + 1}", "type": "debate", "title": f"Debate position {i + 1}: {head}",
                "body": f"Argue a well-reasoned position (#{i + 1} of {n}); steelman alternatives, cite evidence.\n\n{task}",
                "deps": []} for i in range(n)]
        dag.append({"key": "judge", "type": "judge", "title": f"Judge & synthesize: {head}",
                    "body": f"Read all positions, weigh them, and synthesize a decision with rationale.\n\n{task}",
                    "deps": [d["key"] for d in dag]})
        return dag
    if pattern == "consensus":
        dag = [{"key": f"voter{i + 1}", "type": "vote", "title": f"Vote {i + 1}: {head}",
                "body": f"Answer with 'VERDICT: approve' or 'VERDICT: reject' plus a short reason.\n\n{task}", "deps": []}
               for i in range(n)]
        dag.append({"key": "tally", "type": "tally", "title": "Tally votes", "body": "", "deps": [d["key"] for d in dag]})
        return dag
    if pattern == "cross_review":
        return [{"key": "code", "type": "code", "title": f"Implement: {head}", "body": task, "deps": [],
                 "gates": ["review", "red_team"]},
                {"key": "review", "type": "review", "title": f"Review: {head}",
                 "body": "Review the implementation for correctness and style. End with 'VERDICT: approve' or "
                         "'VERDICT: reject' + required changes.", "deps": ["code"]},
                {"key": "red_team", "type": "red_team", "title": f"Find flaws: {head}",
                 "body": "Adversarially look for bugs, security holes and edge cases. 'VERDICT: approve' only if none "
                         "are blocking.", "deps": ["code"]}]
    if pattern == "parallel":
        dag = [{"key": "plan", "type": "plan", "title": f"Plan: {head}",
                "body": f"Write a short shared plan/interfaces for the parts below.\n\n{task}", "deps": []}]
        for i, it in enumerate(items):
            typ = "research" if re.match(r"(?i)(research|investigate|compare|find out)", it) else (
                "test" if re.match(r"(?i)(test|verify)", it) else "code")
            dag.append({"key": f"part{i + 1}", "type": typ, "title": it[:120], "body": it, "deps": ["plan"],
                        "gates": [f"review{i + 1}"]})
            dag.append({"key": f"review{i + 1}", "type": "review", "title": f"Review: {it[:100]}",
                        "body": "Review this part. End with 'VERDICT: approve' or 'VERDICT: reject'.",
                        "deps": [f"part{i + 1}"]})
        dag.append({"key": "summary", "type": "summarize", "title": f"Merge & summarize: {head}",
                    "body": "Merge the approved parts into one coherent result; list leftovers.",
                    "deps": [f"part{i + 1}" for i in range(len(items))]})
        return dag
    # plan -> execute -> verify
    return [{"key": "plan", "type": "plan", "title": f"Plan: {head}", "body": task, "deps": []},
            {"key": "execute", "type": "code", "title": f"Execute: {head}", "body": task, "deps": ["plan"],
             "gates": ["verify"]},
            {"key": "verify", "type": "test", "title": f"Verify: {head}",
             "body": "Verify the result meets the goal. End with 'VERDICT: approve' or 'VERDICT: reject'.",
             "deps": ["execute"]}]


def parse_verdict(text: str) -> str:
    m = re.search(r"(?im)^\s*VERDICT:\s*(approve|approved|pass|reject|rejected|fail)", text or "")
    if m:
        return "approve" if m.group(1).lower() in ("approve", "approved", "pass") else "reject"
    low = (text or "").lower()
    if re.search(r"\b(reject|request(ed)? changes|blocking issue|fails?)\b", low):
        return "reject"
    return "approve"


# ============================================================================ orchestrator
class Orchestrator:
    def __init__(self, store: Store):
        self.s = store
        self.profiles = Profiles(store)
        self.prefs = Preferences(store)

    # ---------------------------------------------------------------- submit
    def submit(self, task: str, *, pattern: str = "auto", project: str | None = None, agent_id: str | None = None,
               n: int = 3, budget: dict | None = None, dag: list[dict] | None = None, title: str = "",
               recurring_id: str | None = None, leftovers: list[dict] | None = None,
               context_pack: str | None = None, dedupe: bool = False, prefer: str | None = None) -> dict:
        """prefer: an agent id the router favours for this job's parts (the agent a big hand-off was addressed to)."""
        task, _ = redact(task)
        if not dag and pattern in ("auto", "llm"):
            dag, how = self._plan(task, prefer)
            pattern = how or pattern
        dag = dag or plan(task, pattern if pattern not in ("llm", "agent-plan") else "auto", n)
        if leftovers:
            for i, lo in enumerate(leftovers):
                dag.append({"key": f"leftover{i + 1}", "type": lo.get("task_type") or "general",
                            "title": f"Leftover: {lo['text'][:100]}", "body": lo["text"], "deps": [],
                            "priority": lo.get("priority", 1), "leftover_id": lo["id"]})
        chosen = self._check(dag)
        if self.paused():
            raise CommonsError(f"orchestration paused: {self.paused()} (resume with set_paused(false))")
        if dedupe and not recurring_id:
            sim = self.similar_jobs(task, project)
            if sim:  # same work already running: join it instead of paying twice
                st = self.status(sim[0]["job_id"], brief=True)
                return st | {"duplicate_of": sim[0]["job_id"], "similarity": sim[0]["similarity"],
                             "text": f"Similar job already running ({sim[0]['similarity']}): " + st["text"]}
        jid = new_id("job")
        if context_pack:  # pass an id, not text: subtasks load the layer they need
            from .contextpacks import ContextStore
            cp = ContextStore(self.s)._pack(context_pack)["id"]
            self.s.x("INSERT OR REPLACE INTO meta VALUES(?, ?)", (f"job_ctx:{jid}", cp))
            self.s.relate(jid, cp, "uses_context")
        if prefer:
            self.s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"prefer:{jid}", prefer))
        pid = self.s.project_id(project)
        budget = budget or {}
        self.s.x("INSERT INTO jobs(id,title,body,pattern,project_id,submitted_by,status,budget,used,result,recurring_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (jid, title or task.strip().splitlines()[0][:120], task, pattern, pid, agent_id, "running",
                  _j(budget), _j({"tokens": 0, "seconds": 0.0, "cost": 0.0}), None, recurring_id, now(), now()))
        self._insert(jid, dag, chosen, {k: (v / max(1, len(dag))) for k, v in budget.items() if isinstance(v, (int, float))})
        if pid:
            self.s.relate(jid, pid, "in_project")
        self.allocate(jid)
        self._auto(jid)
        from .graphlinks import link_job
        link_job(self.s, jid)
        return self.status(jid)

    def _check(self, dag: list[dict]) -> dict:
        """Validate a DAG before anything is written; returns the agents chosen per key: {key: (agent_id, pinned)}.
        agent: chosen by the submitter, retries stay; assign: first try only (delegate, agent planners)."""
        keys = {d["key"] for d in dag}
        by_key = {d["key"]: d for d in dag}
        for d in dag:  # a review/test always waits for the work it checks, or it would approve work not yet done
            for g in d.get("gates", []):
                if g in by_key and d["key"] not in by_key[g].setdefault("deps", []):
                    by_key[g]["deps"] = by_key[g]["deps"] + [d["key"]]
        chosen = {}
        for d in dag:
            for field in ("assign", "agent"):
                if d.get(field):
                    a = self.s.get_agent(d[field])
                    if not a:
                        raise CommonsError(f"subtask {d['key']}: unknown agent {d[field]}")
                    chosen[d["key"]] = (a["id"], field == "agent")
            for k in d.get("deps", []) + d.get("gates", []):
                if k not in keys:
                    raise CommonsError(f"subtask {d['key']} references unknown key {k}")
        _check_acyclic(dag)
        return chosen

    def _insert(self, jid: str, dag: list[dict], chosen: dict, per: dict, prefix: str = "") -> dict:
        """Write a checked DAG into job jid (keys get `prefix`, so parts added later never collide). Returns key -> id."""
        ids = {d["key"]: new_id("sub") for d in dag}
        gate_of = {g: d["key"] for d in dag for g in d.get("gates", [])}
        for d in dag:
            self.s.x("INSERT INTO subtasks(id,job_id,key,type,title,body,deps,gates,gate_of,status,priority,budget,"
                     "max_attempts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (ids[d["key"]], jid, prefix + d["key"], d.get("type", "general"), d.get("title", d["key"]),
                      d.get("body", ""), _j([ids[k] for k in d.get("deps", [])]),
                      _j([ids[k] for k in d.get("gates", [])]),
                      ids[gate_of[d["key"]]] if d["key"] in gate_of else None, "pending", d.get("priority", 0),
                      _j(d.get("budget", per)), d.get("max_attempts", 3), now(), now()))
            self.s.relate(ids[d["key"]], jid, "part_of")
            if d["key"] in chosen:  # set before allocate() runs, so the first wake goes to this agent
                who, pinned = chosen[d["key"]]
                self.s.x("UPDATE subtasks SET assigned_agent=? WHERE id=?", (who, ids[d["key"]]))
                self.s.relate(ids[d["key"]], who, "assigned_to")
                if pinned:
                    self.s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"pin:{ids[d['key']]}", who))
            if d.get("plans"):  # an agent writes this job's plan: its result becomes the parts
                self.s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"planfor:{ids[d['key']]}", jid))
            if d.get("leftover_id"):
                self.s.x("UPDATE leftovers SET status='carried', carried_to=?, updated_at=? WHERE id=?",
                         (ids[d["key"]], now(), d["leftover_id"]))
                self.s.relate(ids[d["key"]], d["leftover_id"], "carries")
        return ids

    def _plan(self, task: str, prefer: str | None = None) -> tuple[list[dict] | None, str | None]:
        """Walk the plan chain: a model plans now, or a live agent plans as the job's first step, or (None, None) and
        the caller uses the keyword rules."""
        from . import planning as P
        from . import settings as _st
        from .providers import ProviderLayer
        pl = ProviderLayer(self.s)
        for entry in _st.get(self.s, "plan_chain"):
            if entry == "rules":
                break
            if entry == "agent":
                who = self._planner_agent(prefer)
                if who:
                    return [{"key": "plan", "type": "plan", "title": f"Plan: {task.strip().splitlines()[0][:100]}",
                             "body": P.agent_brief(self.s, task), "assign": who, "plans": True,
                             "max_attempts": 1}], "agent-plan"  # one try: a failure or timeout falls back to the rules
                continue
            model = pl.role_model(entry[5:]) if entry.startswith("role:") else entry
            if not model or not self.s.q("SELECT 1 FROM providers WHERE name=? AND enabled=1", (model.split("/")[0],)):
                continue
            dag = P.llm(self.s, model, task)
            if dag:
                return dag, "llm"
        return None, None

    def _planner_agent(self, prefer: str | None = None) -> str | None:
        """The live agent to write a plan: the preferred one if it is live with usage to spare, else the best planner."""
        from . import quota as Q
        from .messaging import LIVE_MODES, presence
        ok = lambda aid: presence(self.s, aid)["wake_mode"] in LIVE_MODES and Q.can_take(self.s, aid, "plan")[0]  # noqa: E731
        if prefer and ok(prefer):
            return prefer
        return next((a["id"] for _sc, a in self.profiles.rank("plan") if ok(a["id"])), None)

    def _expand_plan(self, st: dict, result: str) -> int:
        """An agent's plan arrived: add its parts to the job (the keyword rules if it doesn't parse)."""
        from . import planning as P
        job = dict(self.s.q("SELECT * FROM jobs WHERE id=?", (st["job_id"],))[0])
        dag = P.parse(result)
        if not dag:
            self.s.audit(None, "plan.unparsed", st["id"], (result or "")[:300])
            dag = plan(job["body"], "auto")
        ids = self._insert(job["id"], dag, self._check(dag), {}, prefix="p.")
        for d in dag:  # the first parts hang off the plan: they see it, and it never shows up as a job result
            if not d.get("deps"):
                self.s.x("UPDATE subtasks SET deps=? WHERE id=?", (_j([st["id"]]), ids[d["key"]]))
        self.s.x("DELETE FROM meta WHERE key=?", (f"planfor:{st['id']}",))
        self.allocate(job["id"])
        return len(dag)

    def split(self, subtask_id: str, agent_id: str, parts: list) -> dict:
        """The agent holding a subtask hands parts of it to other agents. The parts are routed like any job step (what
        each agent is good at, usage left); the subtask waits for them and comes back to this agent to merge, with
        their results in the claim packet. parts: [{title, body, type?, deps?: [keys of other parts]}] or strings."""
        st = dict((self.s.q("SELECT * FROM subtasks WHERE id=?", (subtask_id,)) or [{}])[0])
        if st.get("claimed_by") != agent_id or st.get("status") != "claimed":
            raise CommonsError("split only a subtask you have claimed")
        if not 2 <= len(parts or []) <= 12:
            raise CommonsError("split into 2 to 12 parts")
        dag = []
        for i, p in enumerate(parts):
            p = {"title": p[:120], "body": p} if isinstance(p, str) else dict(p)
            if not (p.get("title") or p.get("body")):
                raise CommonsError(f"part {i + 1} needs a title or body")
            dag.append({"key": str(p.get("key") or i + 1), "type": p.get("type") or st["type"],
                        "title": (p.get("title") or p["body"])[:120], "body": p.get("body") or p["title"],
                        "deps": [str(k) for k in p.get("deps") or []], **({"agent": p["agent"]} if p.get("agent") else {})})
        chosen = self._check(dag)
        merge = (f"\n\n--- MERGE ---\nYou split this into {len(dag)} parts and other agents did them. Their results "
                 "are in dependency_results: combine them into the final result for this subtask.")
        with self.s.tx():  # parts and the hand-back land together, or not at all if the claim was lost meanwhile
            ids = self._insert(st["job_id"], dag, chosen, {}, prefix=f"{st['key']}.{st['attempts']}.")
            won = self.s.db.execute(
                "UPDATE subtasks SET status='pending', claimed_by=NULL, lease_expires=NULL, assigned_agent=?, deps=?, "
                "body=?, max_attempts=max_attempts+1, updated_at=? WHERE id=? AND claimed_by=? AND status='claimed'",
                (agent_id, _j(json.loads(st["deps"] or "[]") + list(ids.values())), st["body"] + merge, now(),
                 subtask_id, agent_id)).rowcount
            if won != 1:
                raise CommonsError("subtask claim expired or was reassigned before the split")
        for cid in ids.values():
            self.s.relate(cid, subtask_id, "split_from")
        self.s.audit(agent_id, "subtask.split", subtask_id, {"parts": list(ids.values())})
        self.allocate(st["job_id"])
        rows = {r["id"]: r for r in self.s.q(f"SELECT id, title, assigned_agent FROM subtasks WHERE id IN "
                                              f"({','.join('?' * len(ids))})", list(ids.values()))}
        return {"split": subtask_id, "parts": [{"subtask_id": i, "title": rows[i]["title"],
                                                "assigned_to": self.s.label(rows[i]["assigned_agent"])[1]
                                                if rows[i]["assigned_agent"] else None} for i in ids.values()],
                "next": "You are woken to merge when every part is done; their results come with your claim."}

    # ---------------------------------------------------------------- kill switch
    def paused(self) -> str | None:
        r = self.s.q("SELECT value FROM meta WHERE key='paused'")
        return r[0]["value"] if r else None

    def set_paused(self, paused: bool, reason: str = "manual kill switch") -> dict:
        if paused:
            self.s.x("INSERT OR REPLACE INTO meta VALUES('paused', ?)", (reason,))
        else:
            self.s.x("DELETE FROM meta WHERE key='paused'")
        self.s.audit(None, "orchestration.pause" if paused else "orchestration.resume", "", reason)
        return {"paused": paused, "reason": reason if paused else None}

    def cancel(self, job_id: str, reason: str = "cancelled", agent_id: str | None = None) -> dict:
        self.s.x("UPDATE jobs SET status='cancelled', result=?, updated_at=? WHERE id=?", (reason, now(), job_id))
        self.s.x("UPDATE subtasks SET status='cancelled', updated_at=? WHERE job_id=? AND status NOT IN ('done','failed')",
                 (now(), job_id))
        self.s.audit(agent_id, "job.cancel", job_id, reason)
        return self.status(job_id, brief=True)

    def jobs(self, status: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT id FROM jobs" + (" WHERE status=?" if status else "") + " ORDER BY created_at DESC LIMIT ?"
        return [self.status(r["id"]) for r in self.s.q(sql, ([status] if status else []) + [limit])]

    # ---------------------------------------------------------------- allocation
    def _deps_ok(self, st: dict) -> bool:
        for dep in json.loads(st["deps"] or "[]"):
            r = self.s.q("SELECT status, gate_of FROM subtasks WHERE id=?", (dep,))[0]
            if r["status"] == "done":
                continue
            if r["status"] == "awaiting_gate" and st["gate_of"] == dep:
                continue
            return False
        return True

    def _author_of_chain(self, st: dict) -> list[str]:
        """Agents who must not gate this subtask (the author of the gated work)."""
        if not st["gate_of"]:
            return []
        r = self.s.q("SELECT claimed_by, assigned_agent FROM subtasks WHERE id=?", (st["gate_of"],))[0]
        return [x for x in (r["claimed_by"], r["assigned_agent"]) if x]

    def allocate(self, job_id: str) -> None:
        rows = [dict(r) for r in self.s.q("SELECT * FROM subtasks WHERE job_id=? AND status='pending' AND "
                                          "assigned_agent IS NULL AND type<>'tally' ORDER BY created_at", (job_id,))]
        used: dict[str, int] = {}
        for st in rows:
            exclude = json.loads(st["excluded_agents"] or "[]") + self._author_of_chain(st)
            # spread debaters / voters / parallel parts over distinct agents when possible
            siblings = [r["assigned_agent"] for r in self.s.q(
                "SELECT assigned_agent FROM subtasks WHERE job_id=? AND type=? AND assigned_agent IS NOT NULL",
                (job_id, st["type"]))]
            ranked = self.profiles.rank(st["type"], exclude)
            if not ranked:
                continue
            from . import quota as Q
            from . import settings as _st
            fits, resets = ([], []) if _st.get(self.s, "quota_routing") else (list(ranked), [])
            for sc, a in (ranked if _st.get(self.s, "quota_routing") else []):
                ok, until, _w = Q.can_take(self.s, a["id"], st["type"])
                if ok:
                    fits.append((sc, a))
                elif until:
                    resets.append(until)
            if not fits:  # every capable agent is at its limit (after the user's reserve): wait, lose nothing
                self.s.x("UPDATE subtasks SET not_before=? WHERE id=?", (min(resets), st["id"]))
                self.s.audit(None, "subtask.wait_quota", st["id"], {"until": iso(min(resets))})
                continue
            ranked = fits
            from .providers import ProviderLayer
            route = ProviderLayer(self.s).route_for(st["type"])
            if route:  # distributor's routing table nudges allocation
                ranked = [(sc + (0.3 if a["id"] == route["agent_id"] else 0), a) for sc, a in ranked]
            pen = 1.0 if st["type"] in ("debate", "vote") else 0.3
            prefer = self.s.q("SELECT value FROM meta WHERE key=?", (f"prefer:{job_id}",))
            prefer = prefer[0]["value"] if prefer else None
            adj = [(sc - pen * siblings.count(a["id"]) - 0.1 * used.get(a["id"], 0) + self._usage_bonus(a["id"], st["type"])
                    + (0.3 if a["id"] == prefer else 0), a) for sc, a in ranked]
            from .routing import Router
            job = self.s.q("SELECT * FROM jobs WHERE id=?", (job_id,))
            picked = Router(self.s).choose(st, adj, dict(job[0]) if job else None)
            best = (0, picked[0]) if picked else max(adj, key=lambda x: x[0])
            used[best[1]["id"]] = used.get(best[1]["id"], 0) + 1
            self.s.x("UPDATE subtasks SET assigned_agent=? WHERE id=?", (best[1]["id"], st["id"]))
            self.s.relate(st["id"], best[1]["id"], "assigned_to")
            if not self._deps_ok(st):  # ready work is mailed (and woken) by _wake_ready below; heads-up for the rest
                from .messaging import emit
                emit(self.s, best[1]["id"], "task_assigned", ref=st["id"], preview=st["title"] or "",
                     job=job_id, subtask=st["id"])
        self._wake_ready(job_id)

    def _usage_bonus(self, agent_id: str, task_type: str) -> float:
        """More usage left ranks higher (up to +0.3); room that would reset unused within hours adds 0.1. Agents that
        report no windows sit in the middle."""
        from . import quota as Q
        from . import settings as _st
        if not _st.get(self.s, "quota_routing"):
            return 0.0
        room, expiring = Q.headroom(self.s, agent_id, task_type)
        return 0.15 if room is None else 0.3 * room + (0.1 if expiring else 0)

    def _wake_ready(self, job_id: str) -> None:
        """Mail each assigned subtask to its agent once it can actually be claimed (dependencies done). A
        `commons agent daemon` long-polls the mailbox and fires within a second; chat agents see it on their next
        msg_recv. Waking at assignment time instead started agents on reviews of work that didn't exist yet."""
        from .messaging import Messaging
        for st in self.s.q("SELECT * FROM subtasks WHERE job_id=? AND status='pending' AND assigned_agent IS NOT NULL "
                           "AND type<>'tally'", (job_id,)):
            st = dict(st)
            key = f"woke:{st['id']}:{st['attempts']}:{int(st.get('not_before') or 0)}"  # once per attempt and per pause
            if not self._deps_ok(st) or (st.get("not_before") or 0) > now() or self.s.q("SELECT 1 FROM meta WHERE key=?", (key,)):
                continue
            self.s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, st["assigned_agent"]))
            kind = "review" if st["gate_of"] or st["type"] in ("review", "red_team") else "task"
            ev = "debate_turn" if st["type"] == "debate" else None
            Messaging(self.s).send(None, st["assigned_agent"], f"{'debate turn' if ev else kind} ready: {st['title']} "
                                   f"(job {job_id}). Claim it with claim_subtask(subtask_id=\"{st['id']}\").", mtype=kind,
                                   subject=st["title"][:80], meta={"job": job_id, "subtask": st["id"]}, event=ev)

    # ---------------------------------------------------------------- claims
    def claimable(self, agent_id: str, limit: int = 10) -> list[dict]:
        self.reap()
        rows = [dict(r) for r in self.s.q(
            "SELECT s.* FROM subtasks s JOIN jobs j ON j.id=s.job_id WHERE s.status='pending' AND j.status='running' "
            "AND s.type<>'tally' AND (s.assigned_agent=? OR s.assigned_agent IS NULL) AND COALESCE(s.not_before,0)<=? "
            "ORDER BY s.priority DESC, s.created_at", (agent_id, now()))]
        from .scheduler import Scheduler
        rows = Scheduler(self.s).order(rows)  # priorities, fair share, project limits/budgets, job deps, pauses
        out = []
        for st in rows:
            if agent_id in json.loads(st["excluded_agents"] or "[]") or agent_id in self._author_of_chain(st):
                continue
            if self._deps_ok(st):
                out.append(st)
            if len(out) >= limit:
                break
        return out

    def claim(self, agent_id: str, subtask_id: str | None = None, lease_seconds: int = DEFAULT_LEASE) -> dict | None:
        from .telemetry import span
        with span("orchestrator.claim", agent=agent_id):
            return self._claim(agent_id, subtask_id, lease_seconds)

    def _claim(self, agent_id: str, subtask_id: str | None = None, lease_seconds: int = DEFAULT_LEASE) -> dict | None:
        if self.paused():
            return None
        with self.s.lock:
            cands = self.claimable(agent_id, 50)
            if subtask_id:
                cands = [c for c in cands if c["id"] == subtask_id]
            if not cands:
                return None
            if not subtask_id:  # per-agent capacity (profile max_concurrent; agents work several subtasks in parallel)
                prof = json.loads((self.s.q("SELECT profile FROM agents WHERE id=?", (agent_id,)) or [{"profile": "{}"}])[0]["profile"] or "{}")
                cap = int(prof.get("max_concurrent") or os.environ.get("COMMONS_AGENT_DEFAULT_CAPACITY", 4))
                if self.profiles.load(agent_id) >= cap:
                    return None
            st = cands[0]
            job = self.s.q("SELECT * FROM jobs WHERE id=?", (st["job_id"],))[0]
            if self._over_budget(job):
                self.s.x("UPDATE jobs SET status='budget_exceeded', updated_at=? WHERE id=?", (now(), job["id"]))
                self.s.x("UPDATE subtasks SET status='cancelled', updated_at=? WHERE job_id=? AND status IN "
                         "('pending','ready')", (now(), job["id"]))  # nothing left dangling as 'pending' forever
                return None
            cur = self.s.db.execute(
                "UPDATE subtasks SET status='claimed', claimed_by=?, lease_expires=?, attempts=attempts+1, "
                "started_at=?, updated_at=? WHERE id=? AND status='pending'",
                (agent_id, now() + lease_seconds, now(), now(), st["id"]))
            if cur.rowcount != 1:  # someone else won the race: no duplicate work
                return None
        from . import quota as Q
        snap = Q.snapshot(self.s, agent_id)
        if snap:
            self.s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"qsnap:{st['id']}", json.dumps(snap)))
        return self.packet(st["id"], agent_id)

    def packet(self, subtask_id: str, agent_id: str | None = None, max_tokens: int = 1500) -> dict:
        """Everything an agent needs to execute a subtask: brief, dep results, feedback, preferences, context."""
        st = dict(self.s.q("SELECT * FROM subtasks WHERE id=?", (subtask_id,))[0])
        job = dict(self.s.q("SELECT * FROM jobs WHERE id=?", (st["job_id"],))[0])
        dep_results = []
        for d in json.loads(st["deps"] or "[]"):
            r = self.s.q("SELECT key, type, title, result, claimed_by FROM subtasks WHERE id=?", (d,))[0]
            res = r["result"] or ""
            cap = max(200, max_tokens // max(1, len(json.loads(st["deps"])))) * 4
            dep_results.append({"id": d, "key": r["key"], "type": r["type"], "by": self.s.label(r["claimed_by"])[1]
                                if r["claimed_by"] else None, "result": res[:cap] + ("…" if len(res) > cap else "")})
        project = self.s.project_name(job["project_id"])
        prefs = self.prefs.get(project=project, task_type=st["type"], query=st["title"], max_tokens=250)
        ctx = self.s.search(st["title"], project=project, max_tokens=200, limit=5)["text"] if project else ""
        if project:
            from .kb import KB
            dg = KB(self.s).digest(project, max_tokens=160)
            ctx = (dg + "\n\n" + ctx) if dg else ctx
        from .providers import ProviderLayer
        from .skillpool import SkillPool
        route = ProviderLayer(self.s).route_for(st["type"])
        model = None
        if route and agent_id:
            own = {r["model_id"] for r in self.s.q("SELECT model_id FROM agent_models WHERE agent_id=?", (agent_id,))}
            model = route["model_id"] if route["model_id"] in own or not own else sorted(own)[0]
        from .routing import Router
        target = Router(self.s).current(st["id"])
        if target and target["model"]:
            model = target["model"]
        skills = SkillPool(self.s).suggest(f"{st['type']} {st['title']}")
        pack = None  # (was `ctx = None`, which threw away the related memory gathered above)
        cpr = self.s.q("SELECT value FROM meta WHERE key=?", (f"job_ctx:{job['id']}",))
        if cpr:
            from .contextpacks import ContextStore
            ld = ContextStore(self.s).load(cpr[0]["value"], "L1", 1500, model, agent_id=agent_id)
            pack = {"pack_id": ld["pack_id"], "layer": ld["layer"], "tokens": ld["tokens"], "prefix": ld["prefix"] or ld["text"],
                    "hint": f"context_load('{ld['pack_id']}', layer='L2'|'L3') for more"}
        checkpoint = st.get("checkpoint")
        return {"context": pack, "model": model,
                **({"resume_from_checkpoint": checkpoint,
                    "resume_hint": "an earlier attempt paused at its session limit; continue from this checkpoint instead "
                                   "of starting over"} if checkpoint else {}), "effort": (target or {}).get("effort"),
                "routing": {k: target[k] for k in ("decision_id", "difficulty", "mode", "rationale")} if target else None,
                "skills": [f"{x['name']}: {x['description'][:80]}" for x in skills],
                "subtask_id": st["id"], "job_id": job["id"], "job_title": job["title"], "type": st["type"],
                "title": st["title"], "instructions": st["body"], "attempt": st["attempts"],
                "lease_expires": iso(st["lease_expires"]), "budget": json.loads(st["budget"] or "{}"),
                "dependency_results": dep_results, "preferences": prefs["text"], "project": project,
                "related_memory": ctx, "is_gate": bool(st["gate_of"]),
                "split": "Too big for one sitting, or parts others do better? split_subtask(subtask_id, parts) "
                         "hands them out and you merge the results.",
                "protocol": "Return your result. Gate tasks (review/red_team/test/vote) must include a line "
                            "'VERDICT: approve' or 'VERDICT: reject'. Add 'LEFTOVER: <text>' lines for unfinished "
                            "items and 'PREFERENCE: <text>' for user preferences you learned."}

    def pause(self, subtask_id: str, agent_id: str, checkpoint: str, resume_at: float | None = None,
              release: bool = False) -> dict:
        """Park a claimed subtask with its progress (checkpoint) until resume_at (default: the agent's next quota reset).
        The same agent picks it up after the reset; release=True lets any agent with headroom take it now. Nothing
        done so far is lost: the next claim's packet carries `resume_from_checkpoint`."""
        from . import quota as Q
        if not (checkpoint or "").strip():
            raise CommonsError("pause needs a checkpoint: what is done, what is left, where the work is")
        if resume_at is None and not release:
            ok, until, _w = Q.can_take(self.s, agent_id)
            resume_at = until or now() + float(os.environ.get("COMMONS_QUOTA_BACKOFF", "3600"))
        checkpoint, _ = redact(checkpoint)
        with self.s.lock:
            won = self.s.db.execute(
                "UPDATE subtasks SET status='pending', claimed_by=NULL, lease_expires=NULL, checkpoint=?, not_before=?, "
                "assigned_agent=?, updated_at=? WHERE id=? AND claimed_by=? AND status='claimed'",
                (checkpoint[:20000], None if release else resume_at, None if release else agent_id, now(), subtask_id,
                 agent_id)).rowcount
        if won != 1:
            raise CommonsError("subtask is not claimed by you")
        st = self.s.q("SELECT job_id FROM subtasks WHERE id=?", (subtask_id,))[0]
        self.s.audit(agent_id, "subtask.pause", subtask_id, {"resume_at": iso(resume_at) if resume_at else None,
                                                             "release": release})
        if release:
            self.allocate(st["job_id"])
        return {"subtask_id": subtask_id, "paused_until": iso(resume_at) if resume_at and not release else None,
                "released": release, "checkpoint_chars": len(checkpoint)}

    def exhausted(self, agent_id: str, resets_at: float | None = None, checkpoint: str = "", window: str = "self-reported") -> dict:
        """An agent hit its limit: record it, and park everything it holds until the reset with its progress."""
        from . import quota as Q
        resets_at = resets_at or now() + float(os.environ.get("COMMONS_QUOTA_BACKOFF", "3600"))
        Q.report(self.s, agent_id, [{"window": window, "used_pct": 100, "resets_at": resets_at}], source="exhausted")
        held = [r["id"] for r in self.s.q("SELECT id FROM subtasks WHERE claimed_by=? AND status='claimed'", (agent_id,))]
        for sid in held:
            self.pause(sid, agent_id, checkpoint or "(paused: the agent hit its usage limit mid-task; no checkpoint text "
                                                    "was given, so review its last heartbeat/messages)", resets_at)
        return {"agent_id": agent_id, "until": iso(resets_at), "paused": held}

    def quota_wake(self) -> list[str]:
        """After a reset time passes: hand waiting work back out (its own agent if it has headroom again, else
        anyone who does) and wake whoever gets it."""
        from . import quota as Q
        woke = []
        for st in self.s.q("SELECT * FROM subtasks WHERE status='pending' AND not_before IS NOT NULL AND not_before<=?",
                           (now(),)):
            st = dict(st)
            if st["assigned_agent"] and not Q.can_take(self.s, st["assigned_agent"], st["type"])[0]:
                self.s.x("UPDATE subtasks SET assigned_agent=NULL WHERE id=?", (st["id"],))
            self.s.x("UPDATE subtasks SET not_before=NULL WHERE id=?", (st["id"],))
            self.allocate(st["job_id"])
            self._wake_ready(st["job_id"])
            woke.append(st["id"])
        return woke

    def heartbeat(self, subtask_id: str, agent_id: str, lease_seconds: int = DEFAULT_LEASE) -> dict:
        with self.s.lock:  # conditional: a late heartbeat must not extend a lease someone else now holds
            ok = self.s.db.execute("UPDATE subtasks SET lease_expires=?, updated_at=? WHERE id=? AND claimed_by=? "
                                   "AND status='claimed'", (now() + lease_seconds, now(), subtask_id, agent_id)).rowcount
        if ok != 1:
            raise CommonsError("not your active claim")
        return {"subtask_id": subtask_id, "lease_expires": iso(now() + lease_seconds)}

    def _pin(self, subtask_id: str) -> str | None:
        r = self.s.q("SELECT value FROM meta WHERE key=?", (f"pin:{subtask_id}",))
        return r[0]["value"] if r else None

    def reap(self) -> list[str]:
        """Expired leases: retry (exclude the agent that timed out and reassign) or fail after max_attempts."""
        out = []
        for st in self.s.q("SELECT * FROM subtasks WHERE status='claimed' AND lease_expires<?", (now(),)):
            pin = self._pin(st["id"])
            excl = json.loads(st["excluded_agents"] or "[]") + ([] if pin else [st["claimed_by"]])
            with self.s.lock:  # only reap if it is still the same expired claim (no heartbeat/complete in between)
                won = self.s.db.execute(
                    "UPDATE subtasks SET status=?, claimed_by=NULL, assigned_agent=?, excluded_agents=?, updated_at=? "
                    "WHERE id=? AND status='claimed' AND claimed_by=? AND lease_expires<?",
                    ("failed" if st["attempts"] >= st["max_attempts"] else "pending", pin, _j(excl), now(), st["id"],
                     st["claimed_by"], now())).rowcount == 1
            if not won:
                continue
            self.profiles.record_outcome(st["claimed_by"], st["type"], False, subtask_id=st["id"], note="lease timeout")
            from .messaging import emit
            emit(self.s, st["claimed_by"], "lease_expired", ref=st["id"], preview=f"lease expired: {st['title'] or ''}",
                 job=st["job_id"], subtask=st["id"])  # the old holder learns at once that it no longer owns this
            if st["attempts"] >= st["max_attempts"]:
                self._fail(st["id"], "lease expired too many times")
            else:
                self.s.audit(st["claimed_by"], "subtask.timeout", st["id"])
                self.allocate(st["job_id"])
            out.append(st["id"])
        return out

    # ---------------------------------------------------------------- completion & gates
    def complete(self, subtask_id: str, agent_id: str, result: str, **kw) -> dict:
        """Finish a claimed subtask. Runs under the store lock so the reaper, a late heartbeat or a parallel
        completion of a sibling (job budget read-modify-write) can't interleave with it."""
        with self.s.lock:
            prev, self.s.defer_embed = self.s.defer_embed, True  # embed after the lock: ONNX stalls every agent
            try:
                out = self._complete(subtask_id, agent_id, result, **kw)
            finally:
                self.s.defer_embed = prev
        if not prev:
            self.s.reindex(limit=8)  # newest first: the result memory just written
        return out

    def _complete(self, subtask_id: str, agent_id: str, result: str, *, verdict: str | None = None,
                  tokens: int | None = None, cost: float | None = None, leftovers: list[str] | None = None,
                  preferences: list[str] | None = None, success: bool = True, tokens_in: int | None = None,
                  model: str | None = None) -> dict:
        st = dict(self.s.q("SELECT * FROM subtasks WHERE id=?", (subtask_id,))[0])
        if st["claimed_by"] != agent_id or st["status"] != "claimed":
            raise CommonsError("subtask is not claimed by you")
        result, _ = redact(result or "")
        # Decide the race first: the conditional UPDATE is the only check another process (a second worker on its own
        # connection, or a shared Postgres) can't slip past, so usage, budget and leftovers are written by the winner only.
        gates = json.loads(st["gates"] or "[]")
        is_gate = st["gate_of"] is not None or st["type"] in ("vote",)
        v = (verdict or parse_verdict(result)) if is_gate and success else None
        if success:
            won = self.s.db.execute("UPDATE subtasks SET status=?, result=?, verdict=?, updated_at=? WHERE id=? AND "
                                    "claimed_by=? AND status='claimed'", ("awaiting_gate" if gates else "done", result, v,
                                                                          now(), subtask_id, agent_id)).rowcount
        else:  # out of attempts: straight to failed (_fail below also fails the job), else back in the queue
            pin = self._pin(subtask_id)  # pinned work is retried by the agent the submitter chose, never moved
            won = self.s.db.execute("UPDATE subtasks SET status=?, claimed_by=NULL, assigned_agent=?, result=?, "
                                    "excluded_agents=?, updated_at=? WHERE id=? AND claimed_by=? AND status='claimed'",
                                    ("failed" if st["attempts"] >= st["max_attempts"] else "pending", pin, result,
                                     _j(json.loads(st["excluded_agents"] or "[]") + ([] if pin else [agent_id])),
                                     now(), subtask_id, agent_id)).rowcount
        if won != 1:
            raise CommonsError("subtask claim expired or was reassigned before completion")
        snap = self.s.q("SELECT value FROM meta WHERE key=?", (f"qsnap:{subtask_id}",))
        if snap:
            from . import quota as Q
            Q.record_cost(self.s, agent_id, st["type"], json.loads(snap[0]["value"]))
            self.s.x("DELETE FROM meta WHERE key=?", (f"qsnap:{subtask_id}",))
        seconds = now() - (st["started_at"] or now())
        tokens = tokens if tokens is not None else estimate_tokens(result)
        job = dict(self.s.q("SELECT * FROM jobs WHERE id=?", (st["job_id"],))[0])
        used = json.loads(job["used"] or "{}")
        used["tokens"] = used.get("tokens", 0) + tokens
        used["seconds"] = used.get("seconds", 0) + seconds
        from .usage import Usage
        u = Usage(self.s).report(agent_id, tokens_in=tokens_in or 0, tokens_out=tokens, model=model, cost=cost,
                                 seconds=seconds, job_id=job["id"], subtask_id=subtask_id, note=st["type"])
        used["cost"] = round(used.get("cost", 0) + u["cost"], 6)
        used["tokens"] += tokens_in or 0
        self.s.x("UPDATE jobs SET used=?, updated_at=? WHERE id=?", (_j(used), now(), job["id"]))
        project = self.s.project_name(job["project_id"])
        for lo in leftovers or []:
            self.add_leftover(lo, task_type=st["type"], project=project, source_id=subtask_id,
                              recurring_id=job["recurring_id"], agent_id=agent_id)
        for p in preferences or []:
            self.prefs.observe(p, project=project, source=f"agent:{self.s.label(agent_id)[1]}", confidence=0.6,
                               evidence=f"from subtask {subtask_id}")
        if not success:
            self.profiles.record_outcome(agent_id, st["type"], False, subtask_id=subtask_id, tokens=tokens, seconds=seconds)
            if st["attempts"] >= st["max_attempts"]:
                self._fail(subtask_id, result)
            else:
                self.allocate(st["job_id"])
            return self.status(st["job_id"], brief=True)
        item = self.s.add_item("memory", result, title=f"[{st['type']}] {st['title']}", project=project,
                               agent_id=agent_id, tags=["subtask", st["type"]], related_ids=[subtask_id, job["id"]])
        self.s.relate(subtask_id, item["id"], "produced")
        if self.s.q("SELECT 1 FROM meta WHERE key=?", (f"planfor:{subtask_id}",)):  # before _auto closes the job
            self._expand_plan(st, result)
        if st["type"] == "librarian":
            from .librarian import Librarian
            Librarian(self.s).apply_actions(result, agent_id)
        if st["type"] == "judge":
            self.s.record_decision(st["title"].replace("Judge & synthesize: ", ""), result[:500],
                                   agent_id=agent_id, project=project, related_ids=[job["id"], subtask_id])
        if not st["gate_of"]:
            # gated work is scored when its gates decide; ungated work counts as success now
            if not gates:
                self.profiles.record_outcome(agent_id, st["type"], True, subtask_id=subtask_id, tokens=tokens,
                                             seconds=seconds)
        else:
            self.profiles.record_outcome(agent_id, st["type"], True, subtask_id=subtask_id, tokens=tokens, seconds=seconds)
            self._evaluate_gates(st["gate_of"])
        self._auto(st["job_id"])
        self._wake_ready(st["job_id"])  # whoever was waiting on this subtask can start now
        from .graphlinks import link_job
        link_job(self.s, st["job_id"])
        self.s.kb_dirty(job["project_id"])
        return self.status(st["job_id"], brief=True) | {"verdict": v}

    def _evaluate_gates(self, gated_id: str) -> None:
        g = dict(self.s.q("SELECT * FROM subtasks WHERE id=?", (gated_id,))[0])
        gates = [dict(self.s.q("SELECT * FROM subtasks WHERE id=?", (x,))[0]) for x in json.loads(g["gates"])]
        if any(x["status"] != "done" for x in gates):
            return  # wait until every gate has decided
        rejected = [x for x in gates if x["status"] == "done" and x["verdict"] == "reject"]
        if rejected:
            author = g["claimed_by"]
            self.profiles.record_outcome(author, g["type"], False, subtask_id=gated_id, note="gate rejected")
            feedback = "\n\n".join(f"[{x['type']} feedback]\n{x['result']}" for x in rejected)
            for fb in rejected:  # corrections from reviewers are a preference signal too
                self.prefs.extract(fb["result"] or "", project=self.s.project_name(
                    self.s.q("SELECT project_id FROM jobs WHERE id=?", (g["job_id"],))[0]["project_id"]),
                    task_type=g["type"], source="review-feedback")
            if g["attempts"] >= g["max_attempts"]:
                self._fail(gated_id, "quality gate rejected too many times")
                return
            self.s.x("UPDATE subtasks SET status='pending', body=?, assigned_agent=?, claimed_by=NULL, updated_at=? "
                     "WHERE id=?", (g["body"] + "\n\n--- REWORK REQUIRED ---\n" + feedback, author, now(), gated_id))
            for x in gates:  # all gates re-run on the new revision
                self.s.x("UPDATE subtasks SET status='pending', verdict=NULL, claimed_by=NULL, updated_at=? WHERE id=?",
                         (now(), x["id"]))
            self.allocate(g["job_id"])  # route the rework (and wake the author) instead of waiting for a poll
            return
        if all(x["status"] == "done" and x["verdict"] == "approve" for x in gates):
            self.s.x("UPDATE subtasks SET status='done', updated_at=? WHERE id=?", (now(), gated_id))
            self.profiles.record_outcome(g["claimed_by"], g["type"], True, subtask_id=gated_id, note="gates passed")

    def _fail(self, subtask_id: str, why: str) -> None:
        st = dict(self.s.q("SELECT * FROM subtasks WHERE id=?", (subtask_id,))[0])
        if self.s.q("SELECT 1 FROM meta WHERE key=?", (f"planfor:{subtask_id}",)):  # no agent could plan: rules do
            self.s.x("UPDATE subtasks SET status='done', result=?, updated_at=? WHERE id=?",
                     (f"No agent could plan this ({why}); split by the keyword rules.", now(), subtask_id))
            self._expand_plan(st, "")
            return
        self.s.x("UPDATE subtasks SET status='failed', result=COALESCE(result,'')||?, updated_at=? WHERE id=?",
                 (f"\nFAILED: {why}", now(), subtask_id))
        self.s.x("UPDATE jobs SET status='failed', updated_at=? WHERE id=?", (now(), st["job_id"]))

    def _auto(self, job_id: str) -> None:
        """Run automatic steps (vote tallies) and close the job when everything is done."""
        for t in self.s.q("SELECT * FROM subtasks WHERE job_id=? AND type='tally' AND status='pending'", (job_id,)):
            if self._deps_ok(dict(t)):
                deps = json.loads(t["deps"] or "[]")
                votes = [r["verdict"] for r in self.s.q(
                    f"SELECT verdict FROM subtasks WHERE id IN ({','.join('?' * len(deps))})", deps)] if deps else []
                yes = votes.count("approve")
                res = f"{yes}/{len(votes)} approve -> {'APPROVED' if yes * 2 > len(votes) else 'REJECTED'}"
                self.s.x("UPDATE subtasks SET status='done', result=?, verdict=?, updated_at=? WHERE id=?",
                         (res, "approve" if yes * 2 > len(votes) else "reject", now(), t["id"]))
        rows = self.s.q("SELECT status FROM subtasks WHERE job_id=?", (job_id,))
        if rows and all(r["status"] == "done" for r in rows):
            self._merge(job_id)

    def _merge(self, job_id: str) -> None:
        job = dict(self.s.q("SELECT * FROM jobs WHERE id=?", (job_id,))[0])
        subs = [dict(r) for r in self.s.q("SELECT * FROM subtasks WHERE job_id=? ORDER BY created_at", (job_id,))]
        dep_of = {d for s in subs if not s["gate_of"] for d in json.loads(s["deps"] or "[]")}
        sinks = [s for s in subs if s["id"] not in dep_of and not s["gate_of"]] or subs[-1:]
        merged = "\n\n".join(f"## {s['title']}\n{s['result'] or ''}" for s in sinks)
        self.s.x("UPDATE jobs SET status='done', result=?, updated_at=? WHERE id=?", (merged, now(), job_id))
        item = self.s.add_item("memory", merged, title=f"Job result: {job['title']}",
                               project=self.s.project_name(job["project_id"]), agent_id=job["submitted_by"],
                               tags=["job-result"], related_ids=[job_id])
        self.s.relate(job_id, item["id"], "produced")
        if job["submitted_by"]:  # the asker hears back without polling
            try:
                from .messaging import Messaging
                Messaging(self.s).send(None, self.s.label(job["submitted_by"])[1],
                                       f"Done: {job['title']} (job {job_id})\n\n{merged[:6000]}", mtype="message",
                                       subject=f"done: {job['title'][:60]}", meta={"job": job_id})
            except Exception:  # noqa: BLE001 - the result is stored either way
                pass

    def delegate(self, sender: str, recipient: str, body: str, title: str = "", project: str | None = None,
                 reroute: bool = True, plan: bool = True) -> dict:
        """A task one agent hands another becomes a tracked job (it shows as running, has a lease, and is reassigned if
        it stalls). It goes to the recipient when the recipient is live; otherwise to the best live agent, so it never
        waits on an agent that only runs when its user opens it. reroute=False: only the recipient, retries included.
        A big task (several parts or a long brief) is planned across the team instead, favouring the recipient;
        plan=False keeps it whole (skill help: only the helper has the skill)."""
        from . import planning as P
        from .messaging import LIVE_MODES, presence
        rec = self.s.get_agent(recipient)
        title = (title or body.split("\n", 1)[0])[:120]
        if plan and reroute and P.looks_big(body):  # several parts or a long brief: plan it across the team, recipient first
            dag, how = self._plan(body, rec["id"] if rec else None)
            if dag:
                job = self.submit(body, pattern=how, project=project, agent_id=sender, title=title, dag=dag,
                                  prefer=rec["id"] if rec else None)
                parts = [{"subtask_id": x["id"], "title": x["title"], "assigned_to": x.get("agent")}
                         for x in job["subtasks"]]
                return {"job_id": job["job_id"], "split": True, "parts": parts, "rerouted": False,
                        "note": (f"This was big, so Commons split it into {len(parts)} parts across the team"
                                 if how == "llm" else f"This was big, so {parts[0]['assigned_to']} is planning how to "
                                 "split it across the team") + (f", favouring {recipient}" if rec else "")
                        + ". You'll get the combined result in msg_recv when it's done."}
        live = lambda aid: presence(self.s, aid)["wake_mode"] in LIVE_MODES  # noqa: E731
        to = rec["id"] if rec and (live(rec["id"]) or not reroute) else None
        if not to:
            for _sc, a in self.profiles.rank("general", {sender, rec["id"] if rec else ""}):
                if live(a["id"]):
                    to = a["id"]
                    break
        to = to or (rec["id"] if rec else None)
        node = {"key": "only", "type": "general", "title": title, "body": body, "deps": []}
        if to:  # chosen before submit: allocate() would otherwise route it and wake someone else first
            node["assign" if reroute else "agent"] = to
        job = self.submit(body, pattern="parallel", project=project, agent_id=sender, title=title, dag=[node])
        sid = self.s.q("SELECT id FROM subtasks WHERE job_id=?", (job["job_id"],))[0]["id"]
        who = self.s.label(to)[1] if to else None
        return {"job_id": job["job_id"], "subtask_id": sid, "assigned_to": who,
                "rerouted": bool(rec and to != rec["id"]),
                "note": (f"{recipient} isn't running, so Commons gave this to {who}, which is live now. "
                         if rec and to != rec["id"] else "") + "You'll get the result in msg_recv when it's done."}

    def status(self, job_id: str, brief: bool = False) -> dict:
        r = self.s.q("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not r:
            raise CommonsError(f"unknown job {job_id}")
        job = dict(r[0])
        subs = [dict(x) for x in self.s.q("SELECT * FROM subtasks WHERE job_id=? ORDER BY created_at", (job_id,))]
        lines = []
        for s in subs:
            who = self.s.label(s["claimed_by"] or s["assigned_agent"])[1] if (s["claimed_by"] or s["assigned_agent"]) else "-"
            lines.append(f"{s['id']} {s['key']:<10} {s['type']:<9} {s['status']:<13} {who:<14}"
                         f"{(' ' + s['verdict']) if s['verdict'] else ''}  {s['title'][:60]}")
        out = {"job_id": job_id, "title": job["title"], "pattern": job["pattern"], "status": job["status"],
               "budget": json.loads(job["budget"] or "{}"), "used": json.loads(job["used"] or "{}"),
               "text": f"{job['status'].upper()} {job['title']}\n" + "\n".join(lines)}
        if not brief:
            out["subtasks"] = [{k: s[k] for k in ("id", "key", "type", "status", "verdict", "attempts", "title")}
                               | {"agent": self.s.label(s["claimed_by"] or s["assigned_agent"])[1]
                                  if (s["claimed_by"] or s["assigned_agent"]) else None} for s in subs]
            out["result"] = job["result"]
        return out

    def _over_budget(self, job: dict) -> bool:
        b, u = json.loads(job["budget"] or "{}"), json.loads(job["used"] or "{}")
        if b.get("tokens") and u.get("tokens", 0) >= b["tokens"]:
            return True
        if b.get("cost") and u.get("cost", 0) >= b["cost"]:
            return True
        if b.get("seconds") and now() - job["created_at"] >= b["seconds"]:
            return True
        return False

    # ---------------------------------------------------------------- leftovers & recurring
    def add_leftover(self, text: str, *, task_type: str = "general", priority: int = 1, project: str | None = None,
                     source_id: str | None = None, recurring_id: str | None = None, agent_id: str | None = None) -> dict:
        text, _ = redact(text)
        pid = self.s.project_id(project)
        dup = self.s.q("SELECT id FROM leftovers WHERE status='open' AND lower(text)=lower(?) AND project_id IS ? "
                       "AND recurring_id IS ?", (text.strip(), pid, recurring_id))
        if not dup:  # semantic duplicates: same work, different wording
            dup = self._similar_open_leftover(text, pid, recurring_id)
        if dup:  # avoid duplicate work: same unfinished item reported twice
            self.s.x("UPDATE leftovers SET priority=CASE WHEN priority>? THEN priority ELSE ? END, updated_at=? WHERE id=?",
                   (priority, priority, now(), dup[0]["id"]))
            return {"id": dup[0]["id"], "text": text, "duplicate": True}
        from .graphlinks import link_leftover
        lid = new_id("lft")
        self.s.x("INSERT INTO leftovers VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (lid, text.strip(), task_type, priority, pid, source_id, recurring_id, "open", None,
                  agent_id, now(), now()))
        link_leftover(self.s, lid)
        self.s.kb_dirty(pid)
        return {"id": lid, "text": text}

    def _similar_open_leftover(self, text: str, pid, recurring_id) -> list:
        from .embed import cosine
        thr = float(os.environ.get("COMMONS_DUP_SIM", "0.86"))
        rows = self.s.q("SELECT id, text FROM leftovers WHERE status='open' AND project_id IS ? AND recurring_id IS ?",
                        (pid, recurring_id))
        if not rows or not self.s.embedder:
            return []
        vecs = self.s.embedder.embed([text] + [r["text"] for r in rows])
        tq = set(re.findall(r"[a-z0-9]{3,}", text.lower()))
        for r, v in zip(rows, vecs[1:]):
            tr = set(re.findall(r"[a-z0-9]{3,}", r["text"].lower()))
            jac = len(tq & tr) / max(1, len(tq | tr))
            if cosine(vecs[0], v) >= thr and jac >= 0.5:
                return [r]
        return []

    def similar_jobs(self, task: str, project: str | None = None, threshold: float | None = None) -> list[dict]:
        """Running jobs whose task is semantically the same (used by submit to avoid duplicate work)."""
        from .embed import cosine
        if not self.s.embedder:
            return []
        thr = threshold or float(os.environ.get("COMMONS_DUP_SIM", "0.86"))
        pid = self.s.project_id(project) if project else None
        rows = self.s.q("SELECT id, title, body FROM jobs WHERE status='running' AND project_id IS ?", (pid,))
        if not rows:
            return []
        vecs = self.s.embedder.embed([task] + [r["body"] or r["title"] or "" for r in rows])
        tq = set(re.findall(r"[a-z0-9]{3,}", task.lower()))
        out = []
        for r, v in zip(rows, vecs[1:]):
            tr = set(re.findall(r"[a-z0-9]{3,}", (r["body"] or "").lower()))
            sim = cosine(vecs[0], v)
            if sim >= thr and len(tq & tr) / max(1, len(tq | tr)) >= 0.5:
                out.append({"job_id": r["id"], "title": r["title"], "similarity": round(sim, 3)})
        return out

    def resolve_leftover(self, leftover_id: str, note: str = "", agent_id: str | None = None) -> dict:
        self.s.x("UPDATE leftovers SET status='done', updated_at=? WHERE id=?", (now(), leftover_id))
        self.s.audit(agent_id, "leftover.resolve", leftover_id, note)
        return {"id": leftover_id, "status": "done"}

    def leftovers(self, project: str | None = None, status: str = "open", recurring_id: str | None = None) -> list[dict]:
        where, args = ["status=?"], [status]
        if project:
            where.append("project_id=?"); args.append(self.s.project_id(project))
        if recurring_id:
            where.append("recurring_id=?"); args.append(recurring_id)
        return [dict(r) | {"created_at": iso(r["created_at"])} for r in
                self.s.q(f"SELECT * FROM leftovers WHERE {' AND '.join(where)} ORDER BY priority DESC, created_at", args)]

    def schedule(self, name: str, cron: str, task: str, *, pattern: str = "auto", project: str | None = None,
                 budget: dict | None = None, agent_id: str | None = None, catchup: str = "once",
                 max_catchup: int = 3) -> dict:
        """catchup: what to do with runs missed while no scheduler was running —
        'skip' (drop missed slots, wait for the next one), 'once' (one catch-up run), 'all' (one run per missed slot,
        at most max_catchup)."""
        if catchup not in ("skip", "once", "all"):
            raise CommonsError("catchup must be skip|once|all")
        nxt = next_cron(cron, datetime.now())
        rid = new_id("rec")
        self.s.x("INSERT OR REPLACE INTO recurring(id,name,cron,task,pattern,project_id,budget,created_by,enabled,last_run,"
                 "next_run,last_job,created_at,catchup,max_catchup) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (rid, name, cron, redact(task)[0], pattern, self.s.project_id(project), _j(budget or {}), agent_id, 1,
                  None, nxt.timestamp(), None, now(), catchup, max_catchup))
        from .graphlinks import link_recurring
        link_recurring(self.s, rid)
        return {"id": rid, "name": name, "next_run": iso(nxt.timestamp())}

    def run_due(self, force: str | None = None, at: float | None = None) -> list[dict]:
        """Start runs of due recurring tasks; open leftovers from previous runs are carried forward as subtasks."""
        t = at or now()
        rows = self.s.q("SELECT * FROM recurring WHERE enabled=1 AND COALESCE(external,0)=0 AND (next_run<=? OR name=? "
                        "OR id=?)", (t, force, force))
        out = []
        for r in rows:
            # missed-slot accounting (catch-up policy)
            slots, cur = [], r["next_run"] or t
            if force not in (r["name"], r["id"]):
                while cur <= t and len(slots) < 1000:
                    slots.append(cur)
                    cur = next_cron(r["cron"], datetime.fromtimestamp(cur)).timestamp()
            slots = slots or [t]
            policy = (r["catchup"] if "catchup" in r.keys() else None) or "once"
            nxt = next_cron(r["cron"], datetime.fromtimestamp(t))
            missed = len(slots) - 1
            if policy == "skip" and missed:
                runs = []
            elif policy == "all":
                runs = slots[-int(r["max_catchup"] or 3):]
            else:
                runs = [slots[-1]]
            jobs, ncarried = [], 0
            for slot in runs:
                carried = self.leftovers(recurring_id=r["id"])
                ncarried += len(carried)
                job = self.submit(r["task"], pattern=r["pattern"], project=self.s.project_name(r["project_id"]),
                                  agent_id=r["created_by"], budget=json.loads(r["budget"] or "{}"),
                                  title=f"{r['name']} @ {iso(slot)}" + (" (catch-up)" if slot < slots[-1] or missed else ""),
                                  recurring_id=r["id"], leftovers=carried)
                jobs.append(job["job_id"])
            self.s.x("UPDATE recurring SET last_run=?, next_run=?, last_job=COALESCE(?, last_job) WHERE id=?",
                     (t, nxt.timestamp(), jobs[-1] if jobs else None, r["id"]))
            if missed:
                self.s.audit(None, "recurring.missed", r["id"], {"missed": missed, "policy": policy, "ran": len(jobs)})
            from .graphlinks import link_recurring
            link_recurring(self.s, r["id"])
            out.append({"recurring": r["name"], "job_id": jobs[-1] if jobs else None, "jobs": jobs, "missed": missed,
                        "policy": policy, "carried_leftovers": ncarried,
                        "next_run": iso(nxt.timestamp())})
        return out

    def list_recurring(self) -> list[dict]:
        return [dict(r) | {"next_run": iso(r["next_run"]), "last_run": iso(r["last_run"])}
                for r in self.s.q("SELECT * FROM recurring ORDER BY name")]

    # ---------------------------------------------------------------- pickup
    def pickup(self, agent_id: str, project: str | None = None, max_tokens: int = 900) -> dict:
        """What should this agent do next? Ready subtasks, inbox, expertise-ranked leftovers, plus preferences."""
        a = dict(self.s.q("SELECT * FROM agents WHERE id=?", (agent_id,))[0])
        exp = json.loads(a.get("profile") or "{}").get("expertise", {})
        subs = self.claimable(agent_id, 5)
        inbox = self.s.inbox(agent_id, limit=5)
        los = self.leftovers(project)
        t = now()
        ranked = sorted(los, key=lambda lo: -(lo["priority"] + 2 * float(exp.get(lo["task_type"], exp.get("general", 0.3)))
                                              + min(1.0, (t - self.s.q("SELECT created_at FROM leftovers WHERE id=?",
                                                                         (lo["id"],))[0]["created_at"]) / 604800)))
        prefs = self.prefs.get(project=project, max_tokens=max_tokens // 3)
        from .kb import KB
        digest = KB(self.s).digest(project, max_tokens=max_tokens // 4) if project else ""
        lines = []
        from . import board as B  # first, so truncation never cuts what the user is still owed
        bd = B.view(self.s, project, status="open", max_tokens=max_tokens // 3)
        if bd["ids"]:
            lines.append("BOARD (owed to the user: board_update with evidence when done; board for the full view):")
            lines += bd["text"].splitlines()
        if subs:
            lines.append("READY SUBTASKS (claim_subtask):")
            lines += [f"- {s['id']} [{s['type']}] {s['title'][:90]}" for s in subs]
        if inbox:
            lines.append("INBOX:")
            lines += [f"- {i['id']} [{i['kind']}] from {i['from'] or '-'}: {(i['body'] or '')[:90]}" for i in inbox]
        if ranked:
            lines.append("LEFTOVERS (by priority x your expertise):")
            lines += [f"- {lo['id']} [{lo['task_type']}, p{lo['priority']}] {lo['text'][:90]}" for lo in ranked[:8]]
        from .tiers import hot
        hots = hot(self.s, project)
        if hots:
            lines.append("HOT MEMORY (in use now; expand(id) for detail):")
            lines += [f"- {h['id']} [{h['kind']}] {(h['summary'] or '')[:110]}" for h in hots]
        text = "\n".join(lines) or "Nothing pending."
        if prefs["text"]:
            text += "\n\n" + prefs["text"]
        if digest:
            text += "\n\n" + digest
        while estimate_tokens(text) > max_tokens and "\n" in text:
            text = text.rsplit("\n", 1)[0]
        return {"text": text, "subtasks": [s["id"] for s in subs], "inbox": [i["id"] for i in inbox],
                "leftovers": [lo["id"] for lo in ranked[:8]], "preferences": prefs["preferences"], "board": bd["ids"]}


def _check_acyclic(dag: list[dict]) -> None:
    edges = {d["key"]: list(d.get("deps", [])) for d in dag}
    state: dict[str, int] = {}

    def visit(k):
        if state.get(k) == 1:
            raise CommonsError(f"dependency cycle at {k}")
        if state.get(k) == 2:
            return
        state[k] = 1
        for d in edges.get(k, []):
            visit(d)
        state[k] = 2
    for k in edges:
        visit(k)


# ============================================================================ tiny cron
def _cron_field(f: str, lo: int, hi: int) -> set[int]:
    vals: set[int] = set()
    for part in f.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/")
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = map(int, part.split("-"))
        else:
            a = b = int(part)
        vals |= set(range(a, b + 1, step))
    return vals


def next_cron(expr: str, after: datetime) -> datetime:
    """Next time matching a 5-field cron expression (min hour dom month dow; dow 0=Sunday). Supports @hourly/@daily/@weekly."""
    expr = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *"}.get(expr, expr)
    f = expr.split()
    if len(f) != 5:
        raise CommonsError("cron must have 5 fields")
    mi, ho, dom, mo, dow = (_cron_field(f[0], 0, 59), _cron_field(f[1], 0, 23), _cron_field(f[2], 1, 31),
                            _cron_field(f[3], 1, 12), _cron_field(f[4], 0, 7))
    if 7 in dow:
        dow.add(0)
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        if t.month in mo and t.day in dom and ((t.weekday() + 1) % 7) in dow and t.hour in ho and t.minute in mi:
            return t
        t += timedelta(minutes=1)
    raise CommonsError("cron never fires")
