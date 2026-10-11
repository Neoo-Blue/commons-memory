"""User preference learning: preferences with confidence, source, evidence and time decay.

Preferences are observed explicitly (observe_preference), extracted heuristically from corrections/feedback
(extract_preferences; pluggable LLM hook via COMMONS_PREF_EXTRACTOR="<shell cmd reading text on stdin, printing
one preference per line>"), and injected into every task pickup so the user never repeats themselves.
The user can list / edit / confirm / forget / wipe everything.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

from .ids import estimate_tokens, iso, new_id, now
from .redact import redact
from .store import CommonsError, Store

HALF_LIFE_DAYS = float(os.environ.get("COMMONS_PREF_HALF_LIFE_DAYS", "120"))

_RULES = [
    (re.compile(r"\b(?:i|we)\s+(?:really\s+)?(?:prefer|like|want|love)\s+(.+)", re.I), "Prefer {}", 0.6),
    (re.compile(r"\b(?:please\s+)?always\s+(.+)", re.I), "Always {}", 0.6),
    (re.compile(r"\b(?:please\s+)?(?:never|don't|do not|stop|avoid)\s+(.+)", re.I), "Avoid: {}", 0.6),
    (re.compile(r"\bno,?\s+(?:use|do|make it)\s+(.+?)(?:\s+instead.*)?$", re.I), "Prefer {}", 0.5),
    (re.compile(r"\buse\s+(.+?)\s+instead of\s+(.+)", re.I), "Prefer {} over {}", 0.55),
    (re.compile(r"\b(?:i|we)\s+(?:hate|dislike|don't like)\s+(.+)", re.I), "Avoid: {}", 0.6),
]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", s.lower()).strip()


def effective_confidence(conf: float, last: float, t: float | None = None) -> float:
    days = max(0.0, ((t or now()) - last) / 86400)
    return conf * (0.5 ** (days / HALF_LIFE_DAYS))


class Preferences:
    def __init__(self, store: Store):
        self.s = store

    def observe(self, statement: str, *, category: str = "", project: str | None = None, task_type: str | None = None,
                confidence: float = 0.7, source: str = "explicit", evidence: str = "") -> dict:
        statement, _ = redact(statement.strip())
        evidence, _ = redact(evidence or "")
        if not statement:
            raise CommonsError("empty preference")
        pid = self.s.project_id(project)
        norm = _norm(statement)
        r = self.s.q("SELECT * FROM preferences WHERE norm=? AND project_id IS ? AND task_type IS ? AND status<>'forgotten'",
                     (norm, pid, task_type))
        t = now()
        if r:
            p = r[0]
            ev = json.loads(p["evidence"] or "[]") + ([evidence] if evidence else [])
            # repeated observation reinforces: move confidence toward 1
            conf = min(1.0, max(effective_confidence(p["confidence"], p["last_confirmed"]), confidence) + 0.15)
            self.s.x("UPDATE preferences SET confidence=?, evidence=?, observations=observations+1, last_confirmed=?,"
                     " updated_at=?, status='active' WHERE id=?", (conf, json.dumps(ev[-10:]), t, t, p["id"]))
            self._link(p["id"], pid)
            return {"id": p["id"], "statement": p["statement"], "confidence": round(conf, 2), "reinforced": True}
        pid_ = new_id("prf")
        self.s.x("INSERT INTO preferences VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (pid_, statement, norm, category, pid, task_type, confidence, source,
                  json.dumps([evidence] if evidence else []), "active", 1, t, t, t))
        self._link(pid_, pid)
        return {"id": pid_, "statement": statement, "confidence": confidence, "reinforced": False}

    def _link(self, pref_id, project_id):
        from .graphlinks import link_preference
        link_preference(self.s, pref_id)
        self.s.kb_dirty(project_id)

    def extract(self, text: str, *, project: str | None = None, task_type: str | None = None,
                source: str = "feedback", store: bool = True) -> list[dict]:
        """Heuristically extract candidate preferences from a correction/feedback message."""
        cands: list[tuple[str, float]] = []
        hook = os.environ.get("COMMONS_PREF_EXTRACTOR")
        if hook:
            try:
                out = subprocess.run(hook, shell=True, input=text, capture_output=True, text=True, timeout=60).stdout
                cands += [(ln.strip("-* ").strip(), 0.6) for ln in out.splitlines() if ln.strip()]
            except Exception:  # noqa: BLE001 - hook is best effort
                pass
        if not cands:
            for sent in re.split(r"(?<=[.!?;])\s+|\n+", text):
                sent = sent.strip()
                for rx, tmpl, conf in _RULES:
                    m = rx.search(sent)
                    if m:
                        groups = [g.strip().rstrip(".!") for g in m.groups()]
                        cands.append((tmpl.format(*groups), conf))
                        break
        out = []
        for stmt, conf in cands:
            if len(stmt) < 6:
                continue
            out.append(self.observe(stmt, project=project, task_type=task_type, confidence=conf, source=source,
                                    evidence=text[:300]) if store else {"statement": stmt, "confidence": conf})
        return out

    def get(self, project: str | None = None, task_type: str | None = None, query: str = "",
            max_tokens: int = 300, min_confidence: float = 0.3, include_all: bool = False) -> dict:
        pid = self.s.project_id(project) if project else None
        rows = self.s.q("SELECT * FROM preferences WHERE status='active'")
        t = now()
        scored = []
        for r in rows:
            if not include_all:
                if r["project_id"] and r["project_id"] != pid:
                    continue
                if r["task_type"] and task_type and r["task_type"] != task_type:
                    continue
                if r["task_type"] and not task_type:
                    continue
            eff = effective_confidence(r["confidence"], r["last_confirmed"], t)
            if eff < min_confidence and not include_all:
                continue
            spec = (r["project_id"] is not None) + (r["task_type"] is not None)
            rel = 1.0
            if query:
                qw = set(_norm(query).split())
                rel += 0.5 * len(qw & set(r["norm"].split())) / (len(qw) or 1)
            scored.append((eff * rel + 0.05 * spec, eff, r))
        scored.sort(key=lambda x: -x[0])
        lines, prefs, used = [], [], 0
        for _, eff, r in scored:
            scope = "/".join(x for x in (self.s.project_name(r["project_id"]), r["task_type"]) if x) or "global"
            line = f"- {r['statement']} [{scope}, conf {eff:.2f}, {r['id']}]"
            tk = estimate_tokens(line)
            if used + tk > max_tokens and prefs:
                break
            used += tk
            lines.append(line)
            prefs.append({"id": r["id"], "statement": r["statement"], "confidence": round(eff, 3), "scope": scope,
                          "source": r["source"], "observations": r["observations"],
                          "evidence": json.loads(r["evidence"] or "[]"), "last_confirmed": iso(r["last_confirmed"])})
        text = ("User preferences (follow these):\n" + "\n".join(lines)) if lines else ""
        return {"preferences": prefs, "text": text, "tokens": used}

    def confirm(self, pref_id: str, statement: str | None = None) -> dict:
        self._get(pref_id)
        t = now()
        if statement:
            statement, _ = redact(statement)
            self.s.x("UPDATE preferences SET statement=?, norm=? WHERE id=?", (statement, _norm(statement), pref_id))
        self.s.x("UPDATE preferences SET confidence=1.0, last_confirmed=?, updated_at=?, status='active', source="
                 "CASE WHEN source LIKE '%confirmed%' THEN source ELSE source||'+confirmed' END WHERE id=?", (t, t, pref_id))
        return {"id": pref_id, "confirmed": True}

    def forget(self, pref_id: str | None = None, everything: bool = False) -> dict:
        if everything:
            n = self.s.q("SELECT COUNT(*) c FROM preferences")[0]["c"]
            self.s.x("DELETE FROM preferences")
            self.s.x("DELETE FROM relations WHERE src LIKE 'prf_%'")
            return {"deleted": n}
        self._get(pref_id)
        self.s.x("DELETE FROM preferences WHERE id=?", (pref_id,))
        self.s.x("DELETE FROM relations WHERE src=? OR dst=?", (pref_id, pref_id))
        return {"deleted": 1}

    def _get(self, pref_id):
        r = self.s.q("SELECT * FROM preferences WHERE id=?", (pref_id,))
        if not r:
            raise CommonsError(f"unknown preference {pref_id}")
        return r[0]
