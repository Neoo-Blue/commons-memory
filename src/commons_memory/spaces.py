"""Scoped spaces (v0.8, MemPalace "wings/rooms" mapped onto Commons concepts) and the memory audit.

Spaces: ``project:NAME`` (a project), ``topic:NAME`` (a tag / KB topic), ``person:NAME`` (everything linked to a
person entity; people are cross-project). ``space_search`` restricts retrieval to one space.
``audit`` scores how well-organised memory is and lists concrete fixes (librarian input).
"""
from __future__ import annotations

import json
import time

from .store import CommonsError, Store


class Spaces:
    def __init__(self, s: Store):
        self.s = s

    def list(self, scope_type: str | None = None) -> list[dict]:
        out = []
        if scope_type in (None, "project"):
            for r in self.s.q("SELECT p.name, COUNT(i.id) n FROM projects p LEFT JOIN items i ON i.project_id=p.id "
                              "AND i.superseded_by IS NULL GROUP BY p.name ORDER BY n DESC"):
                out.append({"type": "project", "name": r["name"], "items": r["n"]})
        if scope_type in (None, "topic"):
            counts: dict[str, int] = {}
            for r in self.s.q("SELECT tags FROM items WHERE superseded_by IS NULL AND tags NOT IN ('', '[]')"):
                for t in json.loads(r["tags"] or "[]"):
                    counts[t] = counts.get(t, 0) + 1
            out += [{"type": "topic", "name": t, "items": n} for t, n in sorted(counts.items(), key=lambda kv: -kv[1])[:200]]
        if scope_type in (None, "person"):
            for r in self.s.q("SELECT e.name, COUNT(r.id) n FROM entities e LEFT JOIN relations r ON r.dst=e.id OR r.src=e.id "
                              "WHERE e.type='person' GROUP BY e.name ORDER BY n DESC"):
                out.append({"type": "person", "name": r["name"], "items": r["n"]})
        return out

    def search(self, scope: str, query: str, limit: int = 10, max_tokens: int = 600) -> dict:
        typ, _, name = scope.partition(":")
        if not name:
            raise CommonsError("scope must look like project:NAME, topic:NAME or person:NAME")
        if typ == "project":
            return self.s.search(query, project=name, limit=limit, max_tokens=max_tokens)
        if typ == "topic":
            return self.s.search(query, tags=[name.lower()], limit=limit, max_tokens=max_tokens)
        if typ == "person":
            e = self.s.q("SELECT id FROM entities WHERE type='person' AND lower(name)=lower(?)", (name,))
            if not e:
                return {"results": [], "text": f"(no person {name})", "total": 0}
            return self.s.search(query, entity=e[0]["id"], limit=limit, max_tokens=max_tokens)
        raise CommonsError(f"unknown space type {typ!r}")


def audit(s: Store, project: str | None = None) -> dict:
    where, args = "i.superseded_by IS NULL", []
    if project:
        where += " AND i.project_id=?"
        args.append(s.project_id(project))
    total = s.q(f"SELECT COUNT(*) n FROM items i WHERE {where}", args)[0]["n"] or 0
    if not total:
        return {"score": 100, "items": 0, "issues": []}
    orphans = s.q(f"SELECT COUNT(*) n FROM items i WHERE {where} AND (i.tags IS NULL OR i.tags IN ('', '[]')) AND NOT EXISTS "
                  f"(SELECT 1 FROM relations r WHERE r.src=i.id OR r.dst=i.id)", args)[0]["n"]
    no_proj = 0 if project else s.q(f"SELECT COUNT(*) n FROM items i WHERE {where} AND i.project_id IS NULL", args)[0]["n"]
    dups = s.q(f"SELECT COALESCE(SUM(c - 1), 0) n FROM (SELECT COUNT(*) c FROM items i WHERE {where} GROUP BY i.summary "
               f"HAVING COUNT(*) > 1) d", args)[0]["n"]
    conflicts = s.q("SELECT COUNT(*) n FROM conflicts WHERE status='open'")[0]["n"]
    stale = s.q("SELECT COUNT(*) n FROM leftovers WHERE status='open' AND created_at<?", (time.time() - 30 * 86400,))[0]["n"]
    issues = []
    for name, n, w, fix in (("orphans (no tags, no links)", orphans, 0.3, "tag them or link to entities; librarian can auto-tag"),
                            ("items without a project", no_proj, 0.15, "assign a project so scoped search finds them"),
                            ("exact duplicates", dups, 0.3, "run the librarian merge / `commons prune`"),
                            ("open fact conflicts", conflicts, 2.0, "resolve with conflicts(resolve_id, keep_id) or fact_set"),
                            ("leftovers open > 30 days", stale, 1.0, "close, reprioritise or reassign")):
        if n:
            issues.append({"issue": name, "count": int(n), "fix": fix, "penalty": round(min(40, 100 * w * n / max(total, 20)), 1)})
    score = max(0, round(100 - sum(i["penalty"] for i in issues)))
    return {"score": score, "items": total, "issues": issues}
