"""Six-stage workflow: DEFINE (/spec) -> PLAN (/plan) -> BUILD (/build) -> VERIFY (/test) -> REVIEW (/review) -> SHIP (/ship).

One job, a fixed stage spine, each stage gated before the next:
  define      spec with numbered acceptance criteria (AC1, AC2, ...)      gate: you approve it
  plan        PRD, contracts and the BUILD workstreams                     gate: a review by another model family
  build       the lead splits its workstreams into task cards for cheaper workers and merges their diffs
              gates on build, in order: verify (tests for every criterion), two cross-family reviews, then you
              confirm before anything ships; any rejection sends the work back to build with the feedback
  ship        version, changelog, release, deploy, smoke test
Leads plan and integrate; workers do one small, well-specified card. Every card carries its stage and criticality, and
Commons picks tier and effort from difficulty x criticality (routing.CRITICALITY). Build and verify work is only done
with evidence (a commit or diff, or a TESTS:/EVIDENCE: line). A rejected stage that hits its rework cap comes to you
instead of failing the job. `trace()` links each acceptance criterion to its tasks, tests and verdicts; `view()` is
what the dashboard's Workflow page draws.
"""
from __future__ import annotations

import json
import re

from .ids import estimate_tokens, iso
from .store import Store

STAGES = ["define", "plan", "build", "verify", "review", "ship"]
COMMANDS = {"define": "/spec", "plan": "/plan", "build": "/build", "verify": "/test", "review": "/review", "ship": "/ship"}
AC = re.compile(r"(?im)^\s*(?:[-*]\s*)?\**(AC\d+)\**\s*[:.)\]-]\s*(.+)$")
CARD = ("Delegate with task cards: split_subtask(subtask_id, parts) where each part is {title, body, acceptance, "
        "covers: [AC ids], inputs: [workspace paths or memory ids, not pasted text], output, criticality: low|high}. "
        "Split only when the work is more than one sitting and the parts are loosely coupled (at most 3 levels deep, "
        "2 to 12 parts); Commons routes each card by difficulty x criticality.")
EVIDENCE = ("Finish with evidence: complete_subtask(..., repo, branch, commit or diff) and a line 'TESTS: <command> -> "
            "<result>' (or 'EVIDENCE: <path or link>' for non-code work). Work without evidence is refused.")


def _node(key, typ, title, body, stage, level, crit, **kw):
    return {"key": key, "type": typ, "title": title, "body": body,
            "card": {"stage": stage, "level": level, "criticality": crit, **kw.pop("card", {})}, **kw}


def dag(task: str, stages: list[str] | None = None) -> list[dict]:
    """The stage spine for `task`; `stages` runs a contiguous subset (the /spec ... /ship commands)."""
    head = (task.strip().splitlines() or ["the task"])[0][:90]
    want = [s for s in STAGES if not stages or s in stages]
    nodes = [
        _node("define", "spec", f"DEFINE: {head}",
              f"Turn this into a spec in define/spec.md: the problem, users, scope, non-goals, success metrics and open "
              f"questions, and numbered acceptance criteria, one per line as 'AC1: <testable criterion>'. Ask the user "
              f"(board question) only what you can't decide. Research prior art if it helps.\n\n{task}",
              "define", "lead", "high", gates=["define_ok"], card={"output": "define/spec.md"}),
        _node("define_ok", "owner", "Approve the spec", "Read the spec and approve it, or answer with what to change.",
              "define", "owner", "high"),
        _node("plan", "plan", f"PLAN: {head}",
              "From the approved spec (dependency results), write plan/prd.md (architecture, interfaces, milestones, "
              "risks, cost estimate), the contracts the workstreams share, and the BUILD graph: workstreams, each with "
              "its task cards (title, acceptance, covers: [AC ids], criticality). Every acceptance criterion must be "
              "covered by at least one card.\n\n" + task,
              "plan", "lead", "high", deps=["define"], gates=["plan_review"], card={"output": "plan/prd.md"}),
        _node("plan_review", "review", f"Review the plan: {head}",
              "Check the plan covers every acceptance criterion, the contracts are sound and the estimate is credible. "
              "End with 'VERDICT: approve' or 'VERDICT: reject' and what to change.", "plan", "gate", "high"),
        _node("build", "code", f"BUILD lead: {head}",
              "Implement the approved plan (dependency results). You lead: for each workstream either do it yourself "
              f"(small) or hand it out. {CARD} When the parts come back, integrate them and run the tests. {EVIDENCE}",
              "build", "lead", "high", deps=["plan"], gates=["verify", "review_a", "review_b", "ship_ok"],
              card={"output": "build/changes.json", "evidence": True}),
        _node("verify", "test", f"VERIFY: {head}",
              "Run the test suites against the build, add missing tests, reproduce and debug failures. Report one line "
              "per acceptance criterion: 'AC1: PASS <test>' or 'AC1: FAIL <why>'. Any FAIL or missing test means "
              f"'VERDICT: reject' with the failures (the build is reworked); all PASS means 'VERDICT: approve'. {EVIDENCE}",
              "verify", "gate", "high", card={"output": "verify/test-report.md", "evidence": True}),
        _node("review_a", "review", f"Review A: {head}",
              "Review the build's diffs in the job workspace: correctness, security, docs, UX, and that every "
              "acceptance criterion in the traceability table has evidence. End with 'VERDICT: approve' or "
              "'VERDICT: reject' and the required changes.", "review", "gate", "high", deps=["verify"]),
        _node("review_b", "review", f"Review B: {head}",
              "Independent second review (another model family than review A where possible): the same checks. End "
              "with 'VERDICT: approve' or 'VERDICT: reject'.", "review", "gate", "high", deps=["verify"]),
        _node("ship_ok", "owner", "Confirm before shipping",
              "Verified and reviewed. Approve to release and deploy, or answer with what to change first.",
              "ship", "owner", "high", deps=["review_a", "review_b"]),
        _node("ship", "ship", f"SHIP: {head}",
              "Version, changelog and release; deploy, smoke-test, and write the rollback plan. Report the tag, the URL "
              f"and the smoke results in ship/release.md. {EVIDENCE}",
              "ship", "lead", "high", deps=["build", "ship_ok"], card={"output": "ship/release.md", "evidence": True}),
    ]
    keep = {n["key"] for n in nodes if n["card"]["stage"] in want}
    out = []
    for n in nodes:
        if n["key"] not in keep:
            continue
        n["deps"] = [d for d in n.get("deps", []) if d in keep]
        if n.get("gates"):
            n["gates"] = [g for g in n["gates"] if g in keep]
        if n["key"] != STAGES[0] and n["card"]["level"] == "lead" and not n["deps"]:
            n["body"] += f"\n\nThis run starts at {n['card']['stage'].upper()}: the task above is your input."
        out.append(n)
    return out


def card(s: Store, subtask_id: str) -> dict:
    r = s.q("SELECT value FROM meta WHERE key=?", (f"card:{subtask_id}",))
    return json.loads(r[0]["value"]) if r else {}


def criteria(s: Store, job_id: str) -> list[tuple[str, str]]:
    """The acceptance criteria from the DEFINE result (or the task itself when the run started later)."""
    r = s.q("SELECT result FROM subtasks WHERE job_id=? AND key='define'", (job_id,))
    text = (r[0]["result"] if r and r[0]["result"] else "") or s.q("SELECT body FROM jobs WHERE id=?", (job_id,))[0]["body"]
    seen, out = set(), []
    for ac, t in AC.findall(text or ""):
        if ac.upper() not in seen:
            seen.add(ac.upper())
            out.append((ac.upper(), t.strip()[:300]))
    return out


def trace(s: Store, job_id: str) -> list[dict]:
    """Requirement -> tasks -> tests -> verdicts. A criterion with no task or no passing test is a gap."""
    subs = [dict(r) for r in s.q("SELECT id, key, type, status, verdict, result FROM subtasks WHERE job_id=?", (job_id,))]
    out = []
    for ac, text in criteria(s, job_id):
        pat = re.compile(rf"\b{ac}\b", re.I)
        tasks = [x["id"] for x in subs if x["key"] not in ("define", "verify") and x["type"] not in ("review", "owner")
                 and (ac in (card(s, x["id"]).get("covers") or []) or pat.search(x["result"] or ""))]
        tests = "missing"
        for x in subs:
            if x["key"] == "verify" and x["result"]:
                m = re.search(rf"(?im)^\W*{ac}\W+(PASS|FAIL)", x["result"])
                tests = m.group(1).lower() if m else tests
        verdicts = [x["verdict"] for x in subs if x["key"] in ("review_a", "review_b") and x["status"] == "done"]
        out.append({"ac": ac, "text": text, "tasks": tasks, "tests": tests,
                    "verdict": ("reject" if "reject" in verdicts else "approve") if verdicts else None})
    return out


def savings(s: Store, job_id: str) -> dict:
    handed = naive = 0
    for r in s.q("SELECT m.value FROM meta m JOIN subtasks t ON m.key='pk:'||t.id WHERE t.job_id=?", (job_id,)):
        v = json.loads(r["value"])
        handed, naive = handed + v.get("handed", 0), naive + v.get("naive", 0)
    return {"handed": handed, "naive": naive}


def record_packet(s: Store, st: dict, packet: dict) -> None:
    """What this claim handed the agent vs giving it the whole task and every upstream result in full."""
    full = [st["body"] or ""] + [r["result"] or "" for r in s.q(
        f"SELECT result FROM subtasks WHERE id IN ({','.join('?' * len(json.loads(st['deps'] or '[]')))})",
        json.loads(st["deps"] or "[]"))] if json.loads(st["deps"] or "[]") else [st["body"] or ""]
    job = s.q("SELECT body FROM jobs WHERE id=?", (st["job_id"],))[0]["body"] or ""
    handed = estimate_tokens(json.dumps({k: packet.get(k) for k in ("instructions", "dependency_results", "related_memory",
                                                                     "preferences", "card", "traceability")}, default=str))
    naive = estimate_tokens(job + "\n".join(full)) + estimate_tokens(packet.get("related_memory") or "")
    prev = json.loads((s.q("SELECT value FROM meta WHERE key=?", (f"pk:{st['id']}",)) or [{"value": "{}"}])[0]["value"])
    s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"pk:{st['id']}", json.dumps(
        {"handed": prev.get("handed", 0) + handed, "naive": prev.get("naive", 0) + max(naive, handed)})))


def _status(st: dict, gates: list[dict]) -> str:
    if st["status"] == "awaiting_gate":
        return "rework" if st["attempts"] > 1 else "waiting_owner" if any(
            g["type"] == "owner" and g["status"] == "pending" and g["_ready"] for g in gates) else "running"
    if st["status"] == "claimed":
        return "rework" if st["attempts"] > 1 else "running"
    if st["status"] == "pending" and st["attempts"] >= 1:
        return "rework"
    return st["status"]


def view(s: Store, job_id: str) -> dict:
    """Everything the Workflow page draws: the stage spine, the agent tree, traceability and the savings meter."""
    from .orchestrator import Orchestrator
    o = Orchestrator(s)
    row = s.q("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not row:
        from .store import CommonsError
        raise CommonsError(f"no job {job_id!r}: pick one from the list")
    job = dict(row[0])
    subs = {r["id"]: dict(r) for r in s.q("SELECT * FROM subtasks WHERE job_id=? ORDER BY created_at", (job_id,))}
    route = {r["subtask_id"]: dict(r) for r in s.q(
        "SELECT subtask_id, model, level, effort FROM routing_decisions WHERE job_id=? ORDER BY created_at", (job_id,))}
    usage = {r["subtask_id"]: dict(r) for r in s.q(
        "SELECT subtask_id, SUM(tokens_in)+SUM(tokens_out) AS t, SUM(cost) AS c FROM usage WHERE job_id=? GROUP BY subtask_id",
        (job_id,))} if s.q("SELECT 1 FROM usage WHERE job_id=? LIMIT 1", (job_id,)) else {}
    parent = {r["src"]: r["dst"] for r in s.q("SELECT src, dst FROM relations WHERE type='split_from' AND src IN (" +
                                              ",".join("?" * len(subs)) + ")", list(subs))} if subs else {}
    from .workspace import folder, _safe

    def node(x):
        c, rd, u = card(s, x["id"]), route.get(x["id"]) or {}, usage.get(x["id"]) or {}
        tier = c.get("tier") or ({"low": "cheap", "high": "top"}.get(c.get("criticality") or "", "mid") if c else None)
        own = x["type"] == "owner"
        x["_ready"] = own and o._deps_ok(x)
        return {"id": x["id"], "parent": parent.get(x["id"]) or x["gate_of"], "stage": c.get("stage"),
                "level": c.get("level") or ("worker" if x["id"] in parent else "gate" if x["gate_of"] else "lead"),
                "title": x["title"], "status": _status(x, []) if not own else ("waiting_owner" if x["_ready"] and
                                                                              x["status"] == "pending" else x["status"]),
                "agent": "you" if own else (s.label(x["claimed_by"] or x["assigned_agent"])[1]
                                            if (x["claimed_by"] or x["assigned_agent"]) else None),
                "model": rd.get("model"), "tier": None if own else tier, "effort": rd.get("level"),
                "tokens": int(u.get("t") or 0), "cost": round(float(u.get("c") or 0), 4),
                "depth": c.get("depth", 1 if x["id"] in parent or x["gate_of"] else 0),
                "started_at": iso(x["started_at"]) if x.get("started_at") else None,
                "finished_at": iso(x["updated_at"]) if x["status"] == "done" else None,
                "deps": json.loads(x["deps"] or "[]"),
                "artifacts": [{"path": f"{folder(job_id)}/{_safe(x['key'])}/result.md", "kind": "result"}]
                if x["result"] and not own else []}
    nodes = [node(x) for x in subs.values()]
    by = {n["id"]: n for n in nodes}
    stages = []
    for stage in STAGES:
        lead = next((x for x in subs.values() if card(s, x["id"]).get("stage") == stage
                     and card(s, x["id"]).get("level") in ("lead",) and x["id"] not in parent), None)
        gates = [x for x in subs.values() if card(s, x["id"]).get("stage") == stage and x["id"] not in parent
                 and card(s, x["id"]).get("level") in ("gate", "owner")]
        if not lead and not gates:
            continue
        head = lead or gates[0]
        if not lead and len(gates) == 1:
            gates = []  # a stage that is a single check (verify) is its own card, not also a connector
        n = by[head["id"]]
        for g in gates:
            g["_ready"] = by[g["id"]]["status"] == "waiting_owner"
        q = {g["id"]: s.q("SELECT value FROM meta WHERE key=?", (f"ownerq:{g['id']}",)) for g in gates}
        stages.append({"stage": stage, "command": COMMANDS[stage], "status": _status(head, gates) if lead else n["status"],
                       "subtask_id": head["id"], "agent": n["agent"], "model": n["model"], "tier": n["tier"],
                       "effort": n["effort"], "tokens": n["tokens"], "cost": n["cost"], "started_at": n["started_at"],
                       "finished_at": n["finished_at"], "rework": max(0, (head["attempts"] or 0) - 1),
                       "gates": [{"id": g["id"], "type": g["type"], "title": g["title"], "status": by[g["id"]]["status"],
                                  "verdict": g["verdict"], "agent": by[g["id"]]["agent"],
                                  "question_id": q[g["id"]][0]["value"] if q[g["id"]] else None} for g in gates]})
    used = json.loads(job["used"] or "{}")
    return {"job": {"id": job_id, "title": job["title"], "status": job["status"], "pattern": job["pattern"],
                    "created_at": iso(job["created_at"]), "updated_at": iso(job["updated_at"]),
                    "tokens": int(used.get("tokens", 0)), "cost": round(float(used.get("cost", 0)), 4)},
            "stages": stages, "nodes": nodes, "trace": trace(s, job_id), "savings": savings(s, job_id)}


def workflows(s: Store, limit: int = 30) -> list[dict]:
    out = []
    for j in s.q("SELECT id, title, status, updated_at FROM jobs WHERE pattern='sdlc' ORDER BY updated_at DESC LIMIT ?",
                 (limit,)):
        cur = next((st for st in STAGES if s.q(
            "SELECT 1 FROM subtasks t JOIN meta m ON m.key='card:'||t.id WHERE t.job_id=? AND t.status<>'done' AND "
            "m.value LIKE ?", (j["id"], f'%"stage": "{st}"%'))), "done")
        out.append({"id": j["id"], "title": j["title"], "status": j["status"], "stage": cur, "updated_at": iso(j["updated_at"])})
    return out
