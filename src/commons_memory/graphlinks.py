"""Graphify everything: link schedules, runs, decisions, preferences, workflows, debate rounds, review verdicts,
usage, leases and secret refs into the shared graph. Every link function is idempotent (relations are unique on
src/dst/type), so `rebuild()` can backfill old databases and link_* can be called on every write.
"""
from __future__ import annotations

import json

from .store import Store

WORKFLOWS = ["plan_execute_verify", "cross_review", "debate", "consensus", "parallel", "custom", "auto", "llm"]


def _entity(s: Store, type_: str, name: str) -> str:
    return s.upsert_entity(type_, name)


def workflow_id(s: Store, pattern: str) -> str:
    return _entity(s, "workflow", pattern or "custom")


def task_type_id(s: Store, t: str) -> str:
    return _entity(s, "task_type", t or "general")


def link_item(s: Store, iid: str) -> None:
    r = s.q("SELECT * FROM items WHERE id=?", (iid,))
    if not r:
        return
    it = r[0]
    if it["project_id"]:
        s.relate(iid, it["project_id"], "in_project")
    if it["agent_id"]:
        s.relate(iid, it["agent_id"], "authored_by")
    if it["session_id"]:
        s.relate(iid, it["session_id"], "in_session")
    if it["kind"] == "decision":
        if it["agent_id"]:
            s.relate(iid, it["agent_id"], "decided_by")
        # decision "about" any entity/code/item it references
        for e in s.q("SELECT dst FROM relations WHERE src=? AND type IN ('related_to','mentions')", (iid,)):
            if s.q("SELECT 1 FROM entities WHERE id=?", (e["dst"],)):
                s.relate(iid, e["dst"], "about")
    for t in json.loads(it["tags"] or "[]"):
        if t in ("decision", "handoff", "checkpoint", "subtask"):
            continue
    if it["subject"]:
        s.relate(iid, _entity(s, "concept", it["subject"]), "about")


def link_job(s: Store, job_id: str) -> None:
    r = s.q("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not r:
        return
    j = r[0]
    wf = workflow_id(s, j["pattern"])
    s.relate(job_id, wf, "uses_pattern")
    if j["project_id"]:
        s.relate(job_id, j["project_id"], "in_project")
    if j["submitted_by"]:
        s.relate(job_id, j["submitted_by"], "submitted_by")
    if j["recurring_id"]:
        s.relate(job_id, j["recurring_id"], "run_of")
    for st in s.q("SELECT * FROM subtasks WHERE job_id=?", (job_id,)):
        link_subtask(s, st["id"], wf)


def link_subtask(s: Store, sid: str, wf: str | None = None) -> None:
    r = s.q("SELECT * FROM subtasks WHERE id=?", (sid,))
    if not r:
        return
    st = r[0]
    s.relate(sid, st["job_id"], "part_of")
    if wf is None:
        j = s.q("SELECT pattern FROM jobs WHERE id=?", (st["job_id"],))
        wf = workflow_id(s, j[0]["pattern"] if j else "custom")
    s.relate(sid, wf, "uses_pattern")
    s.relate(sid, task_type_id(s, st["type"]), "has_type")
    for d in json.loads(st["deps"] or "[]"):
        s.relate(sid, d, "depends_on")
    if st["assigned_agent"]:
        s.relate(sid, st["assigned_agent"], "assigned_to")
    if st["claimed_by"]:
        s.relate(sid, st["claimed_by"], "executed_by")
    if st["gate_of"]:
        s.relate(sid, st["gate_of"], "reviews")
        if st["verdict"]:
            s.relate(sid, st["gate_of"], "approves" if st["verdict"] == "approve" else "rejects",
                     meta={"verdict": st["verdict"], "attempt": st["attempts"]})
    if st["type"] == "debate":
        for j in s.q("SELECT id FROM subtasks WHERE job_id=? AND type='judge'", (st["job_id"],)):
            s.relate(sid, j["id"], "argues_in")
    if st["type"] == "judge":
        s.relate(sid, st["job_id"], "judges")
    if st["type"] == "vote" and st["verdict"]:
        s.relate(sid, st["job_id"], "votes_" + st["verdict"])
    for r2 in s.q("SELECT src FROM relations WHERE src=? AND type='carries'", (sid,)):
        pass


def link_recurring(s: Store, rid: str) -> None:
    r = s.q("SELECT * FROM recurring WHERE id=?", (rid,))
    if not r:
        return
    rec = r[0]
    s.relate(rid, workflow_id(s, rec["pattern"]), "uses_pattern")
    if rec["project_id"]:
        s.relate(rid, rec["project_id"], "in_project")
    if rec["created_by"]:
        s.relate(rid, rec["created_by"], "created_by")
    for j in s.q("SELECT id FROM jobs WHERE recurring_id=?", (rid,)):
        s.relate(j["id"], rid, "run_of")
    for lo in s.q("SELECT id FROM leftovers WHERE recurring_id=?", (rid,)):
        s.relate(lo["id"], rid, "for_schedule")


def link_leftover(s: Store, lid: str) -> None:
    r = s.q("SELECT * FROM leftovers WHERE id=?", (lid,))
    if not r:
        return
    lo = r[0]
    if lo["project_id"]:
        s.relate(lid, lo["project_id"], "in_project")
    if lo["source_id"] and s._exists(lo["source_id"]):
        s.relate(lid, lo["source_id"], "left_over_from")
    if lo["recurring_id"]:
        s.relate(lid, lo["recurring_id"], "for_schedule")
    if lo["carried_to"]:
        s.relate(lo["carried_to"], lid, "carries")
    if lo["created_by"]:
        s.relate(lid, lo["created_by"], "reported_by")


def link_preference(s: Store, pid: str) -> None:
    r = s.q("SELECT * FROM preferences WHERE id=?", (pid,))
    if not r:
        return
    p = r[0]
    s.relate(pid, p["project_id"] or _entity(s, "scope", "global"), "applies_to")
    if p["task_type"]:
        s.relate(pid, task_type_id(s, p["task_type"]), "applies_to")
    src = (p["source"] or "")
    if src.startswith("agent:"):
        a = s.get_agent(src.split(":", 1)[1].split("+")[0])
        if a:
            s.relate(pid, a["id"], "learned_from")


def usage_node(rowid: int) -> str:
    return f"usg_{rowid}"


def link_usage(s: Store, rowid: int) -> None:
    r = s.q("SELECT * FROM usage WHERE id=?", (rowid,))
    if not r:
        return
    u = r[0]
    n = usage_node(rowid)
    for col, rel in (("job_id", "billed_to"), ("project_id", "billed_to"), ("agent_id", "spent_by"),
                     ("subtask_id", "billed_to")):
        if u[col]:
            s.relate(n, u[col], rel, meta={"cost": u["cost"]})
    if u["model"]:
        s.relate(n, _entity(s, "model", u["model"]), "used_model")


def link_secret_ref(s: Store, ref: str) -> None:
    r = s.q("SELECT * FROM secret_refs WHERE ref=?", (ref,))
    if not r:
        return
    sr = r[0]
    if sr["project_id"]:
        s.relate(ref, sr["project_id"], "in_project")
    if sr["created_by"]:
        s.relate(ref, sr["created_by"], "registered_by")
    for a in json.loads(sr["allowed_agents"] or "[]"):
        ag = s.get_agent(a)
        if ag:
            s.relate(ref, ag["id"], "allowed_for")


def link_lease(s: Store, lid: str) -> None:
    r = s.q("SELECT * FROM leases WHERE id=?", (lid,))
    if not r:
        return
    le = r[0]
    s.relate(lid, le["ref"], "lease_of")
    if le["agent_id"]:
        s.relate(lid, le["agent_id"], "held_by")


def link_session(s: Store, sid: str) -> None:
    r = s.q("SELECT * FROM sessions WHERE id=?", (sid,))
    if not r:
        return
    se = r[0]
    if se["project_id"]:
        s.relate(sid, se["project_id"], "in_project")
    if se["agent_id"]:
        s.relate(sid, se["agent_id"], "run_by")


def rebuild(s: Store) -> dict:
    """Backfill every link for every record. Safe to run repeatedly."""
    before = s.q("SELECT COUNT(*) c FROM relations")[0]["c"]
    for w in ("plan_execute_verify", "cross_review", "debate", "consensus", "parallel"):
        workflow_id(s, w)
    with s.lock:
        s.db.execute("BEGIN")
        try:
            for r in s.q("SELECT id FROM items"):
                link_item(s, r["id"])
            for r in s.q("SELECT id FROM sessions"):
                link_session(s, r["id"])
            for r in s.q("SELECT id FROM jobs"):
                link_job(s, r["id"])
            for r in s.q("SELECT id FROM recurring"):
                link_recurring(s, r["id"])
            for r in s.q("SELECT id FROM leftovers"):
                link_leftover(s, r["id"])
            for r in s.q("SELECT id FROM preferences"):
                link_preference(s, r["id"])
            for r in s.q("SELECT id FROM usage"):
                link_usage(s, r["id"])
            for r in s.q("SELECT ref FROM secret_refs"):
                link_secret_ref(s, r["ref"])
            for r in s.q("SELECT id FROM leases"):
                link_lease(s, r["id"])
            s.db.execute("COMMIT")
        except Exception:
            s.db.execute("ROLLBACK")
            raise
    after = s.q("SELECT COUNT(*) c FROM relations")[0]["c"]
    return {"relations_before": before, "relations_after": after, "added": after - before}


# ------------------------------------------------------------------ scoped views
def subgraph(s: Store, scope: str, ident: str, depth: int = 2, limit: int = 600) -> dict:
    """Graph view centred on a project | job | workflow | node (any id) | schedule."""
    if scope == "project":
        root = s.project_id(ident) if not ident.startswith("prj_") else ident
    elif scope == "workflow":
        root = workflow_id(s, ident) if not ident.startswith("ent_") else ident
    else:
        root = ident
    if scope == "project":
        # everything inside the project + one hop out (agents, workflows, code links)
        members = {r["src"] for r in s.q("SELECT src FROM relations WHERE dst=? AND type IN ('in_project','contains',"
                                         "'billed_to','applies_to')", (root,))}
        members |= {r["dst"] for r in s.q("SELECT dst FROM relations WHERE src=? AND type='contains'", (root,))}
        members.add(root)
        nodes = set(members)
        edges = []
        for m in list(members)[: limit * 2]:
            for r in s.q("SELECT src,dst,type FROM relations WHERE src=?", (m,)):
                if r["dst"] in members or depth > 1:
                    edges.append(dict(r))
                    nodes.add(r["dst"])
            if len(edges) > limit * 3:
                break
    else:
        nb = s.neighbors(root, depth=depth, limit=limit * 3)
        nodes, edges = set(nb["nodes"]), nb["edges"]
    nodes = list(nodes)[: limit]
    keep = set(nodes)
    edges = [e for e in edges if e["src"] in keep and e["dst"] in keep]
    uniq = {(e["src"], e["dst"], e["type"]): e for e in edges}
    return {"root": root, "scope": scope, "nodes": {n: dict(zip(("type", "name"), s.label(n))) for n in keep},
            "edges": list(uniq.values())}
