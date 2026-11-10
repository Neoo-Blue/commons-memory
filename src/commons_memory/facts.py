"""Temporal facts (v0.8): validity windows on facts and relations.

A fact is ``subject.predicate = value`` with ``valid_from``/``valid_to``. Setting a new value closes the previous
one at exactly the new ``valid_from`` (it is superseded, never deleted), so ``get(as_of=...)`` answers "what was true
then" and ``history`` shows the full timeline. Backfilling an older value slots it in before the later one.
Relations carry the same window (``end_relation``) so the graph can be queried as of a date.
Canon subjects (product:, work:, company, vendor:; canon.py) have ONE global timeline whatever the caller's project:
one predicate form, values that say something, no value set ahead of time, and an end that is recorded as a change.
"""
from __future__ import annotations

import json

from .canon import SKEW, blank, dump, is_canon, predicate_of, same, settle_set
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
        """Facts about subject (one project's timeline when scoped; pid None = unscoped facts; a Canon subject always
        has its one global timeline), minus those in projects the caller can't `need`."""
        sql = "SELECT * FROM items WHERE kind='fact' AND subject=?"
        args = [subject]
        if predicate:
            sql += " AND predicate=?"
            args.append(predicate)
        if scoped and not is_canon(subject):
            sql += " AND project_id IS ?" if pid is None else " AND project_id=?"
            args.append(pid)
        rows = [dict(r) for r in self.s.q(sql + " ORDER BY COALESCE(valid_from, created_at)", args)]
        if need == "write" and is_canon(subject):  # one timeline: the guard decides who writes Canon, and no hidden
            return rows                             # row may survive as a second current value
        hidden = self.s.hidden_ids([r["id"] for r in rows], need)
        return [r for r in rows if r["id"] not in hidden]

    def set(self, subject: str, predicate: str, value, valid_from=None, project: str | None = None,
            agent_id: str | None = None, decision: str | None = None, evidence: str | None = None) -> dict:
        """decision: the record_decision that set it; evidence: how the agent knows. Both are kept with the fact.
        A Canon fact set again to its current value (in any form), with no decision behind it, changes nothing; a new
        current Canon value, or any value a decision sets, settles the open conflicts on it."""
        canon = is_canon(subject)
        if canon:
            predicate = predicate_of(subject, predicate)
            if blank(value):
                raise CommonsError(f"{subject}.{predicate}: a Canon value says something (text, a list, [] for "
                                   "none, an object); fact_invalidate ends a value")
        if canon or not isinstance(value, str):
            project, value = (None if canon else project), dump(value)
        vf = _t(valid_from) or now()
        if canon and vf > now() + SKEW:
            raise CommonsError(f"{subject}.{predicate}: Canon is the current truth, so it takes no valid_from in the "
                               "future: set it once it is true")
        vf = min(vf, now()) if canon else vf
        pid = self.s.project_id(project) if project else None
        self.s.check_project(pid, "write")
        rows = [r for r in self._rows(subject, predicate, pid, scoped=True, need="write")
                if r["superseded_by"] != "invalidated" or r["valid_to"]]
        cur = [o for o in rows if (o["valid_from"] or o["created_at"]) <= vf and (o["valid_to"] is None or o["valid_to"] > vf)]
        if canon and not decision and valid_from in (None, "") and cur and all(same(o["value"], value) for o in cur):
            return {"id": cur[-1]["id"], "unchanged": True, "valid_from": iso(cur[-1]["valid_from"] or cur[-1]["created_at"]),
                    "valid_to": None, "closed": [], "old": cur[-1]["value"]}
        later = [r for r in rows if (r["valid_from"] or r["created_at"]) > vf]
        vt = min((r["valid_from"] or r["created_at"]) for r in later) if later else None
        meta = {k: v for k, v in (("decision", decision), ("evidence", evidence)) if v}
        with self.s.tx():  # the new value and the closing of the old one: together or not at all
            r = self.s.add_item("fact", f"{subject} {predicate} {value}", project=project, agent_id=agent_id, meta=meta or None,
                                subject=subject, predicate=predicate, value=value, check_conflicts=False)
            new = r["id"]
            for o in cur:
                self.s.x("UPDATE items SET valid_to=?, superseded_by=? WHERE id=?", (vf, new, o["id"]))
                self.s.relate(new, o["id"], "supersedes", {"at": vf})
            self.s.x("UPDATE items SET valid_from=?, valid_to=?, superseded_by=? WHERE id=?",
                     (vf, vt, later[0]["id"] if later else None, new))
            if decision:
                self.s.relate(decision, new, "sets", agent_id=agent_id)
        if (canon or decision) and not later:  # the current value now: the conflicts on it and on what it closed end
            settle_set(self.s, subject, predicate, [o["id"] for o in cur], agent_id, decision or new)
        return {"id": new, "valid_from": iso(vf), "valid_to": iso(vt) if vt else None, "closed": [o["id"] for o in cur],
                "old": cur[-1]["value"] if cur else None}

    def invalidate(self, subject: str, predicate: str, at=None, project: str | None = None,
                   agent_id: str | None = None, evidence: str | None = None) -> dict:
        """End the current value at `at` (never before it began). A Canon value ends now or earlier, never later, and
        the end is recorded (who, evidence, when; an audit row too), so changes() reports it as old -> None."""
        canon, t = is_canon(subject), _t(at) or now()
        if canon:
            predicate, project = predicate_of(subject, predicate), None
            if t > now() + SKEW:
                raise CommonsError(f"{subject}.{predicate}: Canon is the current truth: end a value now, not later")
            t = min(t, now())
        pid = self.s.find_project(project) if project else None
        if project and not pid:
            raise CommonsError(f"no such project {project!r}")
        self.s.check_project(pid, "write")
        cur = [r for r in self._rows(subject, predicate, pid, scoped=True, need="write") if r["superseded_by"] is None]
        if not cur:
            raise CommonsError(f"no current fact {subject}.{predicate}")
        from .redact import redact
        rec, ev, ends = now(), redact(evidence)[0] if evidence else None, []
        for r in cur:
            ends.append(max(t, r["valid_from"] or r["created_at"] or t))  # a window never closes before it opens
            if not canon:
                self.s.x("UPDATE items SET valid_to=?, superseded_by='invalidated' WHERE id=?", (ends[-1], r["id"]))
                continue
            meta = json.loads(r["meta"] or "{}") | {"ended": {"at": rec, "by": agent_id, "evidence": ev}}
            self.s.x("UPDATE items SET valid_to=?, superseded_by='invalidated', updated_at=?, meta=? WHERE id=?",
                     (ends[-1], rec, json.dumps(meta, ensure_ascii=False), r["id"]))
            self.s.audit(agent_id, "fact.invalidate", r["id"], {"subject": subject, "predicate": predicate,
                                                                "valid_to": ends[-1], "evidence": ev})
        return {"invalidated": [r["id"] for r in cur], "at": iso(max(ends))}

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
