"""Temporal facts (v0.8): validity windows on facts and relations.

A fact is ``subject.predicate = value`` with ``valid_from``/``valid_to``. Setting a new value closes the previous
one at exactly the new ``valid_from`` (it is superseded, never deleted), so ``get(as_of=...)`` answers "what was true
then" and ``history`` shows the full timeline. Backfilling an older value slots it in before the later one.
Relations carry the same window (``end_relation``) so the graph can be queried as of a date.
"""
from __future__ import annotations

from .store import CommonsError, Store, _parse_time, iso, now

COLUMNS = [("items", "valid_from", "REAL"), ("items", "valid_to", "REAL"),
           ("relations", "valid_from", "REAL"), ("relations", "valid_to", "REAL")]


def add_columns(db, columns=None):
    for t, c, typ in (columns or COLUMNS):
        try:
            cols = {r[1] for r in db.execute(f"PRAGMA table_info({t})")}
        except Exception:  # noqa: BLE001
            cols = set()
        if not cols:  # postgres: information_schema
            try:
                cols = {r[0] for r in db.execute("SELECT column_name FROM information_schema.columns WHERE table_name=?", (t,))}
            except Exception:  # noqa: BLE001
                cols = set()
            if c not in cols:
                try:
                    db.execute(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {c} DOUBLE PRECISION")
                except Exception:  # noqa: BLE001
                    pass
            continue
        if c not in cols:
            db.execute(f"ALTER TABLE {t} ADD COLUMN {c} {typ}")


def _t(v) -> float | None:
    if v in (None, ""):
        return None
    return float(v) if isinstance(v, (int, float)) else _parse_time(str(v))


class Facts:
    def __init__(self, s: Store):
        self.s = s

    def _rows(self, subject, predicate=None, pid: str | None = None, scoped: bool = False, need: str = "read"):
        """Facts about subject (one project's timeline when scoped; pid None = unscoped facts), minus those in
        projects the caller can't `need`."""
        sql = "SELECT * FROM items WHERE kind='fact' AND subject=?"
        args = [subject]
        if predicate:
            sql += " AND predicate=?"
            args.append(predicate)
        if scoped:
            sql += " AND project_id IS ?" if pid is None else " AND project_id=?"
            args.append(pid)
        rows = [dict(r) for r in self.s.q(sql + " ORDER BY COALESCE(valid_from, created_at)", args)]
        hidden = self.s.hidden_ids([r["id"] for r in rows], need)
        return [r for r in rows if r["id"] not in hidden]

    def set(self, subject: str, predicate: str, value: str, valid_from=None, project: str | None = None,
            agent_id: str | None = None) -> dict:
        vf = _t(valid_from) or now()
        pid = self.s.project_id(project) if project else None
        self.s.check_project(pid, "write")
        rows = [r for r in self._rows(subject, predicate, pid, scoped=True, need="write")
                if r["superseded_by"] != "invalidated" or r["valid_to"]]
        later = [r for r in rows if (r["valid_from"] or r["created_at"]) > vf]
        vt = min((r["valid_from"] or r["created_at"]) for r in later) if later else None
        r = self.s.add_item("fact", f"{subject} {predicate} {value}", project=project, agent_id=agent_id,
                            subject=subject, predicate=predicate, value=value, check_conflicts=False)
        new = r["id"]
        closed = []
        for o in rows:
            ofrom = o["valid_from"] or o["created_at"]
            if ofrom <= vf and (o["valid_to"] is None or o["valid_to"] > vf):
                self.s.x("UPDATE items SET valid_to=?, superseded_by=? WHERE id=?", (vf, new, o["id"]))
                self.s.relate(new, o["id"], "supersedes", {"at": vf})
                closed.append(o["id"])
        self.s.x("UPDATE items SET valid_from=?, valid_to=?, superseded_by=? WHERE id=?",
                 (vf, vt, later[0]["id"] if later else None, new))
        return {"id": new, "valid_from": iso(vf), "valid_to": iso(vt) if vt else None, "closed": closed}

    def invalidate(self, subject: str, predicate: str, at=None, project: str | None = None) -> dict:
        t = _t(at) or now()
        pid = self.s.find_project(project) if project else None
        if project and not pid:
            raise CommonsError(f"no such project {project!r}")
        self.s.check_project(pid, "write")
        cur = [r for r in self._rows(subject, predicate, pid, scoped=True, need="write") if r["superseded_by"] is None]
        if not cur:
            raise CommonsError(f"no current fact {subject}.{predicate}")
        for r in cur:
            self.s.x("UPDATE items SET valid_to=?, superseded_by='invalidated' WHERE id=?", (t, r["id"]))
        return {"invalidated": [r["id"] for r in cur], "at": iso(t)}

    def get(self, subject: str, predicate: str | None = None, as_of=None, project: str | None = None) -> list[dict]:
        rows = self._rows(subject, predicate, self.s.find_project(project), scoped=bool(project))
        if as_of in (None, ""):
            rows = [r for r in rows if r["superseded_by"] is None and (r["valid_to"] is None or r["valid_to"] > now())]
        else:
            t = _t(as_of)
            rows = [r for r in rows if (r["valid_from"] or r["created_at"]) <= t and (r["valid_to"] is None or r["valid_to"] > t)]
        return [self._fmt(r) for r in rows]

    def history(self, subject: str, predicate: str | None = None, project: str | None = None) -> list[dict]:
        return [self._fmt(r) for r in self._rows(subject, predicate, self.s.find_project(project), scoped=bool(project))]

    def _fmt(self, r):
        return {"id": r["id"], "subject": r["subject"], "predicate": r["predicate"], "value": r["value"],
                "valid_from": iso(r["valid_from"] or r["created_at"])[:19],
                "valid_to": iso(r["valid_to"])[:19] if r["valid_to"] else None,
                "current": r["superseded_by"] is None and r["valid_to"] is None}

    # ---- relations with validity
    def end_relation(self, src: str, dst: str, type_: str, at=None) -> dict:
        t = _t(at) or now()
        n = self.s.q("SELECT id FROM relations WHERE src=? AND dst=? AND type=?", (src, dst, type_))
        if not n:
            raise CommonsError("no such relation")
        self.s.x("UPDATE relations SET valid_to=? WHERE id=?", (t, n[0]["id"]))
        return {"id": n[0]["id"], "valid_to": iso(t)}

    def relations_as_of(self, node: str, as_of=None) -> list[dict]:
        t = _t(as_of) or now()
        rows = self.s.q("SELECT * FROM relations WHERE (src=? OR dst=?) AND COALESCE(valid_from, created_at)<=? "
                        "AND (valid_to IS NULL OR valid_to>?)", (node, node, t, t))
        return [dict(r) for r in rows]
