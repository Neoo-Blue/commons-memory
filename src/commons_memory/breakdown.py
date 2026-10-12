"""Task breakdown of a job: the planning step, the DAG, and per-subtask target/effort/rationale/timeline/cost/outputs."""
from __future__ import annotations

import json

from .store import CommonsError, Store


def _j(x, d):
    try:
        return json.loads(x) if x else d
    except (TypeError, ValueError):
        return d


def breakdown(s: Store, job_id: str, result_chars: int = 600) -> dict:
    j = s.q("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not j:
        raise CommonsError(f"no job {job_id}")
    j = dict(j[0])
    name = lambda a: s.label(a)[1] if a else None  # noqa: E731
    subs = [dict(r) for r in s.q("SELECT * FROM subtasks WHERE job_id=? ORDER BY created_at", (job_id,))]
    key_of = {x["id"]: x["key"] for x in subs}
    nodes, edges = [], []
    for st in subs:
        dec = [dict(r) for r in s.q("SELECT * FROM routing_decisions WHERE subtask_id=? ORDER BY created_at", (st["id"],))]
        use = s.q("SELECT COALESCE(SUM(tokens_in),0) ti, COALESCE(SUM(tokens_out),0) tou, COALESCE(SUM(cost),0) c "
                  "FROM usage WHERE subtask_id=?", (st["id"],))[0]
        tl = [{"at": r["ts"], "by": name(r["agent_id"]), "event": r["action"], "detail": (r["detail"] or "")[:200]}
              for r in s.q("SELECT * FROM audit WHERE target=? ORDER BY ts", (st["id"],))]
        for ts, ev in ((st["created_at"], "created"), (st["started_at"], "started"),
                       (st["updated_at"] if st["status"] in ("done", "failed", "cancelled") else None, st["status"])):
            if ts:
                tl.append({"at": ts, "by": None, "event": ev, "detail": ""})
        tl.sort(key=lambda x: x["at"])
        try:
            from .drive import Drive
            arts = Drive(s, backend=object()).artifacts(st["id"])  # metadata only; no blob access needed
        except Exception:  # noqa: BLE001
            arts = []
        last = dec[-1] if dec else {}
        nodes.append({
            "id": st["id"], "key": st["key"], "type": st["type"], "title": st["title"], "status": st["status"],
            "priority": st["priority"], "agent": name(st["claimed_by"] or st["assigned_agent"] or last.get("agent_id")),
            "model": last.get("model"), "effort": last.get("level"), "difficulty": last.get("difficulty"),
            "rationale": last.get("rationale"), "attempts": st["attempts"], "max_attempts": st["max_attempts"],
            "escalations": [{"attempt": d["attempt"], "model": d["model"], "effort": d["level"], "outcome": d["outcome"],
                             "agent": name(d["agent_id"])} for d in dec],
            "gate_of": key_of.get(st["gate_of"]) if st["gate_of"] else None, "verdict": _j(st["verdict"], st["verdict"]),
            "tokens": {"in": use["ti"], "out": use["tou"]}, "cost": round(float(use["c"] or 0), 5),
            "result": (st["result"] or "")[:result_chars], "result_truncated": len(st["result"] or "") > result_chars,
            "timeline": tl, "artifacts": arts, "excluded_agents": [name(a) for a in _j(st["excluded_agents"], [])]})
        for d in _j(st["deps"], []):
            edges.append({"from": key_of.get(d, d), "to": st["key"], "kind": "dep"})
        if st["gate_of"]:
            edges.append({"from": key_of.get(st["gate_of"]), "to": st["key"], "kind": "review"})
    plan = [{"at": r["ts"], "by": name(r["agent_id"]), "event": r["action"], "detail": (r["detail"] or "")[:2000]}
            for r in s.q("SELECT * FROM audit WHERE target=? ORDER BY ts", (job_id,))]
    counts: dict[str, int] = {}
    for n in nodes:
        counts[n["status"]] = counts.get(n["status"], 0) + 1
    return {"job": {"id": j["id"], "title": j["title"], "body": (j["body"] or "")[:2000], "pattern": j["pattern"],
                    "status": j["status"], "priority": j.get("priority"), "project": s.project_name(j["project_id"]) if j["project_id"] else None,
                    "budget": _j(j["budget"], {}), "used": _j(j["used"], {}), "after": _j(j.get("after_jobs"), []),
                    "submitted_by": name(j["submitted_by"]), "created_at": j["created_at"], "updated_at": j["updated_at"]},
            "planning": {"pattern": j["pattern"], "events": plan,
                         "summary": f"{len(nodes)} subtasks, {len(edges)} edges, pattern {j['pattern']}"},
            "nodes": nodes, "edges": edges, "counts": counts,
            "totals": {"cost": round(sum(n["cost"] for n in nodes), 5),
                       "tokens": sum(n["tokens"]["in"] + n["tokens"]["out"] for n in nodes)}}
