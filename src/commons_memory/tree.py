"""Org-chart / outline view of the whole workspace: a navigable hierarchy with stable index paths.

Hierarchies (roots):
  workspace : Workspace > Projects > {Jobs, Workflows, Sessions, Knowledge base, Code, Schedules, Skills,
              Memories, Leftovers} > ... > items      (plus `misc` for unassigned memories)
  agents    : Agents > models they can use > tasks they executed
  skills    : Skills > versions / used by agents / used in tasks

Index paths like `1.2.3` address nodes; children are ordered by creation time (then name), so existing paths
stay stable as new records are appended. Children are computed lazily, one layer at a time.
"""
from __future__ import annotations

import json
from collections import Counter

from .ids import estimate_tokens
from .store import CommonsError, Store

GROUPS = [("jobs", "Jobs & tasks"), ("workflows", "Workflows"), ("sessions", "Sessions"), ("kb", "Knowledge base"),
          ("code", "Code"), ("schedules", "Schedules"), ("skills", "Skills used"), ("memories", "Memories"),
          ("leftovers", "Leftovers")]
ROOTS = {"workspace": "Workspace", "agents": "Agents", "skills": "Skill library"}


def N(key: str, label: str, kind: str, rid: str | None = None, **extra) -> dict:
    return {"key": key, "label": label, "kind": kind, "id": rid, **extra}


class Tree:
    def __init__(self, store: Store):
        self.s = store
        self._labels: dict[str, str] = {}

    # ------------------------------------------------------------------ children
    def node(self, key: str) -> dict:
        if key in ROOTS:
            return N(key, ROOTS[key], "root")
        kind, _, rest = key.partition(":")
        if kind in ("prj",):
            return N(key, self.s.project_name(rest) or "misc", "project", rest if rest != "misc" else None)
        typ, name = self.s.label(rest.split(":")[-1]) if rest else ("", key)
        return N(key, name, kind, rest.split(":")[-1])

    def children(self, key: str) -> list[dict]:
        s = self.s
        kind, _, rest = key.partition(":")
        if key == "workspace":
            out = [N(f"prj:{r['id']}", r["name"], "project", r["id"])
                   for r in s.q("SELECT id, name FROM projects ORDER BY created_at, name")]
            if s.q("SELECT 1 FROM items WHERE project_id IS NULL LIMIT 1"):
                out.append(N("prj:misc", "misc (unassigned)", "project"))
            return out
        if key == "agents":
            return [N(f"agt:{r['id']}", r["name"], "agent", r["id"], platform=r["platform"])
                    for r in s.q("SELECT id, name, platform FROM agents WHERE name NOT LIKE 'provider:%' "
                                 "ORDER BY created_at, name")]
        if key == "skills":
            return [N(f"skl:{r['id']}", f"{r['name']} v{r['active_version'] or '-'}", "skill", r["id"])
                    for r in s.q("SELECT id, name, active_version FROM skills ORDER BY created_at, name")]
        if kind == "prj":
            pid = None if rest == "misc" else rest
            out = []
            for g, label in GROUPS:
                n = self._group_count(pid, g)
                if n:
                    out.append(N(f"grp:{rest}:{g}", label, "group", None, count=n))
            return out
        if kind == "grp":
            prj, _, g = rest.partition(":")
            return self._group(None if prj == "misc" else prj, prj, g)
        if kind == "job":
            return [N(f"sub:{r['id']}", f"{r['key']} · {r['type']} · {r['status']}" + (f" · {r['verdict']}" if r["verdict"] else ""),
                      "subtask", r["id"]) for r in s.q("SELECT * FROM subtasks WHERE job_id=? ORDER BY created_at", (rest,))]
        if kind == "sub":
            out = [N(f"itm:{r['dst']}", s.label(r["dst"])[1], s.label(r["dst"])[0], r["dst"])
                   for r in s.q("SELECT dst FROM relations WHERE src=? AND type='produced'", (rest,))]
            out += [N(f"sub:{r['src']}", "gate: " + s.label(r["src"])[1], "subtask", r["src"])
                    for r in s.q("SELECT src FROM relations WHERE dst=? AND type='reviews'", (rest,))]
            return out
        if kind == "wf":
            prj, _, wf = rest.partition(":")
            return [N(f"job:{r['id']}", r["title"], "job", r["id"]) for r in s.q(
                "SELECT id, title FROM jobs WHERE pattern=? AND project_id IS ? ORDER BY created_at",
                (wf, None if prj == "misc" else prj))]
        if kind == "ses":
            return [N(f"itm:{r['id']}", f"{r['kind']}: {r['title'][:60]}", r["kind"], r["id"])
                    for r in s.q("SELECT id, kind, title FROM items WHERE session_id=? ORDER BY created_at", (rest,))]
        if kind == "kbs":
            bucket, _, sec = rest.partition("|")
            r = s.q("SELECT citations FROM kb_pages WHERE bucket=? AND section=?", (bucket, sec))
            return [N(f"itm:{c}", s.label(c)[1], s.label(c)[0], c) for c in (json.loads(r[0]["citations"]) if r else [])]
        if kind == "dir":
            prj, _, path = rest.partition(":")
            return self._code(prj, path)
        if kind in ("fil", "cls"):
            rel = "defines"
            return [N(f"cls:{r['id']}" if r["type"] == "class" else f"sym:{r['id']}", f"{r['name']} ({r['type']}, L{r['line']})",
                      r["type"], r["id"]) for r in s.q(
                f"SELECT e.* FROM relations r JOIN entities e ON e.id=r.dst WHERE r.src=? AND r.type=? "
                f"ORDER BY e.line, e.name", (rest, rel))]
        if kind == "rec":
            return [N(f"job:{r['id']}", r["title"], "job", r["id"])
                    for r in s.q("SELECT id, title FROM jobs WHERE recurring_id=? ORDER BY created_at", (rest,))]
        if kind == "mk":
            prj, _, k = rest.partition(":")
            return [N(f"itm:{r['id']}", r["title"][:80], k, r["id"]) for r in s.q(
                "SELECT id, title FROM items WHERE project_id IS ? AND kind=? AND superseded_by IS NULL ORDER BY created_at",
                (None if prj == "misc" else prj, k))]
        if kind == "agt":
            out = [N(f"mdl:{rest}:{r['model_id']}", r["model_id"], "model", None)
                   for r in s.q("SELECT model_id FROM agent_models WHERE agent_id=? ORDER BY model_id", (rest,))]
            out.append(N(f"agtask:{rest}", "tasks executed", "group", None,
                         count=s.q("SELECT COUNT(*) c FROM subtasks WHERE claimed_by=?", (rest,))[0]["c"]))
            return out
        if kind == "mdl":
            agent, _, model = rest.partition(":")
            rows = s.q("SELECT DISTINCT subtask_id FROM usage WHERE agent_id=? AND model=? AND subtask_id IS NOT NULL",
                       (agent, model))
            return [N(f"sub:{r['subtask_id']}", s.label(r["subtask_id"])[1], "subtask", r["subtask_id"]) for r in rows]
        if kind == "agtask":
            return [N(f"sub:{r['id']}", f"{r['type']}: {r['title'][:60]} · {r['status']}", "subtask", r["id"])
                    for r in s.q("SELECT id, type, title, status FROM subtasks WHERE claimed_by=? ORDER BY created_at", (rest,))]
        if kind == "skl":
            out = [N(f"skv:{r['id']}", f"v{r['version']} · {r['status']}", "skill_version", r["id"])
                   for r in s.q("SELECT id, version, status FROM skill_versions WHERE skill_id=? ORDER BY version", (rest,))]
            name = s.q("SELECT name FROM skills WHERE id=?", (rest,))[0]["name"]
            ent = s.upsert_entity("skill", name)
            out += [N(f"agt:{r['dst']}", "used by " + s.label(r["dst"])[1], "agent", r["dst"])
                    for r in s.q("SELECT dst FROM relations WHERE src=? AND type='used_by'", (ent,))]
            out += [N(f"sub:{r['dst']}", "used in " + s.label(r["dst"])[1], "subtask", r["dst"])
                    for r in s.q("SELECT dst FROM relations WHERE src=? AND type='used_in'", (ent,))]
            return out
        return []

    def _group_count(self, pid, g) -> int:
        s = self.s
        if g == "jobs":
            return s.q("SELECT COUNT(*) c FROM jobs WHERE project_id IS ?", (pid,))[0]["c"]
        if g == "workflows":
            return s.q("SELECT COUNT(DISTINCT pattern) c FROM jobs WHERE project_id IS ?", (pid,))[0]["c"]
        if g == "sessions":
            return s.q("SELECT COUNT(*) c FROM sessions WHERE project_id IS ?", (pid,))[0]["c"]
        if g == "kb":
            return s.q("SELECT COUNT(*) c FROM kb_pages WHERE bucket=?", (s.project_name(pid) if pid else "misc",))[0]["c"]
        if g == "code":
            return s.q("SELECT COUNT(*) c FROM entities WHERE project_id=? AND type='file'", (pid,))[0]["c"] if pid else 0
        if g == "schedules":
            return s.q("SELECT COUNT(*) c FROM recurring WHERE project_id IS ?", (pid,))[0]["c"]
        if g == "skills":
            return len(self._group(pid, pid or "misc", "skills"))
        if g == "memories":
            return s.q("SELECT COUNT(*) c FROM items WHERE project_id IS ? AND superseded_by IS NULL", (pid,))[0]["c"]
        if g == "leftovers":
            return s.q("SELECT COUNT(*) c FROM leftovers WHERE project_id IS ? AND status='open'", (pid,))[0]["c"]
        return 0

    def _group(self, pid, prj, g) -> list[dict]:
        s = self.s
        if g == "jobs":
            return [N(f"job:{r['id']}", f"{r['title'][:70]} · {r['status']}", "job", r["id"])
                    for r in s.q("SELECT id, title, status FROM jobs WHERE project_id IS ? ORDER BY created_at", (pid,))]
        if g == "workflows":
            return [N(f"wf:{prj}:{r['pattern']}", r["pattern"], "workflow", None, count=r["n"]) for r in s.q(
                "SELECT pattern, COUNT(*) n, MIN(created_at) t FROM jobs WHERE project_id IS ? GROUP BY pattern ORDER BY t", (pid,))]
        if g == "sessions":
            return [N(f"ses:{r['id']}", f"{r['goal'][:60]} · {r['status']}", "session", r["id"])
                    for r in s.q("SELECT id, goal, status FROM sessions WHERE project_id IS ? ORDER BY created_at", (pid,))]
        if g == "kb":
            from .kb import SECTIONS, TITLES
            bucket = s.project_name(pid) if pid else "misc"
            have = {r["section"] for r in s.q("SELECT section FROM kb_pages WHERE bucket=?", (bucket,))}
            return [N(f"kbs:{bucket}|{sec}", TITLES[sec], "kb_section", None) for sec in SECTIONS if sec in have]
        if g == "code":
            return self._code(prj, "")
        if g == "schedules":
            return [N(f"rec:{r['id']}", f"{r['name']} ({r['cron']})", "schedule", r["id"])
                    for r in s.q("SELECT id, name, cron FROM recurring WHERE project_id IS ? ORDER BY created_at", (pid,))]
        if g == "skills":
            subs = [r["id"] for r in s.q("SELECT s.id FROM subtasks s JOIN jobs j ON j.id=s.job_id WHERE j.project_id IS ?", (pid,))]
            if not subs:
                return []
            ph = ",".join("?" * len(subs))
            ents = s.q(f"SELECT DISTINCT r.src FROM relations r WHERE r.type='used_in' AND r.dst IN ({ph})", subs)
            out = []
            for e in ents:
                name = s.label(e["src"])[1]
                sk = s.q("SELECT id, active_version FROM skills WHERE name=?", (name,))
                if sk:
                    out.append(N(f"skl:{sk[0]['id']}", f"{name} v{sk[0]['active_version']}", "skill", sk[0]["id"]))
            return out
        if g == "memories":
            return [N(f"mk:{prj}:{r['kind']}", r["kind"], "kind", None, count=r["n"]) for r in s.q(
                "SELECT kind, COUNT(*) n, MIN(created_at) t FROM items WHERE project_id IS ? AND superseded_by IS NULL "
                "GROUP BY kind ORDER BY t", (pid,))]
        if g == "leftovers":
            return [N(f"itm:{r['id']}", f"p{r['priority']} {r['text'][:70]}", "leftover", r["id"]) for r in s.q(
                "SELECT id, text, priority FROM leftovers WHERE project_id IS ? AND status='open' ORDER BY created_at", (pid,))]
        return []

    def _code(self, prj: str, path: str) -> list[dict]:
        files = self.s.q("SELECT id, path FROM entities WHERE project_id=? AND type='file' ORDER BY path", (prj,))
        pre = path + "/" if path else ""
        dirs, out = Counter(), []
        for f in files:
            if not f["path"].startswith(pre):
                continue
            rest = f["path"][len(pre):]
            if "/" in rest:
                dirs[rest.split("/")[0]] += 1
            else:
                out.append(N(f"fil:{f['id']}", rest, "file", f["id"]))
        return [N(f"dir:{prj}:{pre}{d}", d + "/", "dir", None, count=n) for d, n in sorted(dirs.items())] + out

    # ------------------------------------------------------------------ navigation
    def resolve(self, ref: str) -> tuple[str, str]:
        """root name | node key | index path ('1.2.3' or 'agents/2.1') -> (key, path)."""
        if ref in ROOTS or not ref:
            return ref or "workspace", ""
        root, path = ("workspace", ref)
        if "/" in ref:
            root, path = ref.split("/", 1)
        if root in ROOTS and all(p.isdigit() for p in path.split(".") if p):
            key = root
            for p in path.split("."):
                ch = self.children(key)
                i = int(p) - 1
                if not 0 <= i < len(ch):
                    raise CommonsError(f"no node at {ref}")
                key = ch[i]["key"]
                self._labels[key] = ch[i]["label"]
            return key, path
        return ref, ""

    def find(self, query: str, root: str = "workspace", max_depth: int = 6, limit: int = 10) -> list[dict]:
        """Breadth-first search by label; returns index paths + breadcrumbs so callers can jump-expand."""
        q = query.lower()
        hits, frontier = [], [(root, "", [ROOTS.get(root, root)])]
        for _ in range(max_depth):
            nxt = []
            for key, path, crumbs in frontier:
                for i, c in enumerate(self.children(key), 1):
                    p = f"{path}.{i}" if path else str(i)
                    cr = crumbs + [c["label"]]
                    if q in c["label"].lower() or (c.get("id") and q == c["id"].lower()):
                        hits.append({"path": p, "key": c["key"], "label": c["label"], "kind": c["kind"],
                                     "breadcrumbs": cr, "id": c.get("id")})
                        if len(hits) >= limit:
                            return hits
                    nxt.append((c["key"], p, cr))
            frontier = nxt[:4000]
        return hits

    def outline(self, ref: str = "workspace", depth: int = 2, max_tokens: int = 800, max_children: int = 25) -> dict:
        key, base = self.resolve(ref)
        root_label = ROOTS.get(key) or self._labels.get(key) or self.node(key)["label"]
        lines = [f"{base or ref.split('/')[0] if '/' in ref else base or ''} {root_label}".strip()]
        used = estimate_tokens(lines[0])
        truncated = False
        nodes = []
        prefix_root = (ref.split("/", 1)[0] + "/") if "/" in ref and ref.split("/", 1)[0] in ROOTS else \
            ("" if key == "workspace" or not base else "")
        rootname = ref.split("/", 1)[0] if "/" in ref else (key if key in ROOTS else "workspace")

        def walk(k, path, d):
            nonlocal used, truncated
            ch = self.children(k)
            for i, c in enumerate(ch[:max_children], 1):
                p = f"{path}.{i}" if path else str(i)
                cnt = c.get("count")
                if cnt is None and d >= depth:
                    cnt = len(self.children(c["key"])) or None
                ln = f"{'  ' * (p.count('.') - (base.count('.') + 1 if base else 0))}{p} [{c['kind']}] {c['label']}" + \
                     (f" ({cnt})" if cnt else "") + (f" <{c['id']}>" if c.get("id") else "")
                if used + estimate_tokens(ln) > max_tokens:
                    truncated = True
                    return
                used += estimate_tokens(ln)
                lines.append(ln)
                nodes.append({"path": (rootname + "/" if rootname != "workspace" else "") + p, **c})
                if d < depth:
                    walk(c["key"], p, d + 1)
                    if truncated:
                        return
            if len(ch) > max_children:
                lines.append(f"{'  ' * (path.count('.') + 1 if path else 0)}… {len(ch) - max_children} more")
        walk(key, base, 1)
        if truncated:
            lines.append(f"… truncated at max_tokens={max_tokens}; call tree(root='<path>') to drill down")
        hint = f"Paths are stable addresses: tree(root='{'' if rootname == 'workspace' else rootname + '/'}1.2') " \
               f"drills down; expand(<id>) opens a record."
        return {"root": ref or "workspace", "key": key, "text": "\n".join(lines), "nodes": nodes, "hint": hint,
                "tokens": used, "truncated": truncated}

    # ------------------------------------------------------------------ export
    def to_mermaid(self, ref: str = "workspace", depth: int = 3, max_nodes: int = 200) -> str:
        key, _ = self.resolve(ref)
        lines, n = ["flowchart TD", f'  n0["{(ROOTS.get(key) or self.node(key)["label"]).replace(chr(34), "")}"]'], [0]

        def walk(k, pid, d):
            for c in self.children(k):
                if n[0] >= max_nodes:
                    return
                n[0] += 1
                me = f"n{n[0]}"
                cnt = c.get("count")
                lbl = f"{c['kind']}: {c['label']}".replace('"', "'")[:60] + (f" ({cnt})" if cnt else "")
                lines.append(f'  {me}["{lbl}"]')
                lines.append(f"  {pid} --> {me}")
                if d < depth:
                    walk(c["key"], me, d + 1)
        walk(key, "n0", 1)
        return "\n".join(lines) + "\n"

    def to_markdown(self, ref: str = "workspace", depth: int = 4) -> str:
        o = self.outline(ref, depth=depth, max_tokens=10 ** 7, max_children=10 ** 6)
        out = [f"# {ROOTS.get(o['key'], o['key'])}", ""]
        for nd in o["nodes"]:
            lvl = nd["path"].split("/")[-1].count(".")
            out.append(f"{'  ' * lvl}- **{nd['path']}** {nd['label']} _({nd['kind']})_" + (f" `{nd['id']}`" if nd.get("id") else ""))
        return "\n".join(out) + "\n"
