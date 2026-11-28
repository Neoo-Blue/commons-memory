"""Design ideology: one place where every agent learns how this user wants things to look.

Three layers, all in the existing stores:
- rules: design preferences (the `preferences` table, category "design:<section>[@<surface>]"), so they decay,
  reinforce, show on the Preferences page and reach every pickup like any other preference;
- feedback: the user's own words about a design, captured automatically from every agent's prompts (hooks and turn())
  as items tagged design-feedback, until a rebuild folds them in;
- profile: the distilled ideology (markdown, versioned as items of kind design_profile), written by the
  design_distiller model role or by any agent through design_profile_set.

Agents call design_profile(surface) before visual work and design_feedback() when the user reacts to a design.
"""
from __future__ import annotations

import json
import re

from .ids import estimate_tokens, iso, now
from .store import Store

SECTIONS = {
    "hard_no": "Hard no (never do this)",
    "strong": "Strong preferences",
    "tendency": "Tendencies (depend on the surface)",
    "liked": "Liked examples",
    "rejected": "Rejected examples",
    "vocabulary": "What the user's words mean",
    "process": "Process rules (how to check before showing)",
}
SURFACES = ("marketing", "dashboard", "app", "tool", "email", "mobile", "brand")
PROFILE_KIND = "design_profile"
FEEDBACK_TAG = "design-feedback"

# the user is talking about how something looks...
_DESIGN = re.compile(
    r"\b(design\w*|ugly|beautiful|pretty|stylish|style|font|fonts|typeface|typography|colou?rs?|palette|theme|"
    r"dark mode|layout|animation|animated|motion|cartoon\w*|elegant|generic|taste|premium|cheap|minimal\w*|"
    r"readable|unreadable|ui|ux|interface|aesthetic|gradient|icons?|logo|hero|spacing|cramped|cluttered|"
    r"look(s|ing)?|feel(s)?|vibe|landing page|homepage)\b", re.I)
# ...and is reacting to it, not just asking for a feature
_REACT = re.compile(
    r"\b(i (really )?(like|love|hate|prefer|want)|don'?t like|do not like|not like|looks? (so |too |very |a bit )?"
    r"\w+|feels? (so |too |a bit )?\w+|too (simple|busy|dense|small|tiny|generic|plain|dark|bright|much)|"
    r"ugly|beautiful|generic|no taste|unreadable|hard to read|cartoon\w*|elegant|professional|cheap|boring|"
    r"never|always|make (it|them) (more )?\w+|better|worse|odd|weird|bad|horrible|perfect|great|nice)\b", re.I)


def is_design_feedback(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > 1500 or t.startswith(("<", "CANON", "[Request interrupted")):
        return False
    return bool(_DESIGN.search(t) and _REACT.search(t))


def _category(section: str, surface: str = "") -> str:
    section = section if section in SECTIONS else "strong"
    return f"design:{section}" + (f"@{surface.strip().lower()}" if surface.strip() else "")


def _split(category: str) -> tuple[str, str]:
    body = (category or "")[len("design:"):]
    sec, _, surf = body.partition("@")
    return sec or "strong", surf


class Design:
    def __init__(self, store: Store, prefs=None):
        from .prefs import Preferences
        self.s = store
        self.prefs = prefs or Preferences(store)

    # ------------------------------------------------------------ write
    def feedback(self, text: str, *, section: str = "", surface: str = "", project: str | None = None,
                 evidence: str = "", source: str = "agent", confidence: float | None = None) -> dict:
        """A design rule or reaction. Repeats reinforce (a second rejection makes it a hard no in effect)."""
        section = section if section in SECTIONS else "strong"
        conf = confidence if confidence is not None else (0.9 if section == "hard_no" else 0.75)
        return self.prefs.observe(text, category=_category(section, surface), project=project, confidence=conf,
                                  source=source, evidence=evidence) | {"section": section, "surface": surface}

    def capture(self, text: str, *, project: str | None = None, agent_id: str | None = None,
                session_id: str | None = None) -> str | None:
        """Called on every captured user prompt from every agent: keeps the user's words about a design."""
        if not is_design_feedback(text):
            return None
        text = " ".join(text.split())
        if self.s.q("SELECT 1 FROM items WHERE kind='observation' AND content=? LIMIT 1", (text,)):
            return None
        return self.s.add_item("observation", text, title=f"[design feedback] {text[:70]}", project=project,
                               agent_id=agent_id, session_id=session_id, tags=[FEEDBACK_TAG, "design"],
                               meta={"source": "auto-capture"}, check_conflicts=False)["id"]

    def profile_set(self, body: str, *, agent_id: str | None = None, sources: str = "") -> dict:
        body = (body or "").strip()
        if not body:
            from .store import CommonsError
            raise CommonsError("empty design profile")
        v = self._version() + 1
        r = self.s.add_item(PROFILE_KIND, body, title=f"Design ideology v{v}", agent_id=agent_id,
                            tags=["design", "design-profile"], meta={"version": v, "sources": sources[:500]},
                            check_conflicts=False)
        return {"id": r["id"], "version": v}

    # ------------------------------------------------------------ read
    def latest(self) -> dict | None:
        r = self.s.q("SELECT * FROM items WHERE kind=? AND superseded_by IS NULL ORDER BY created_at DESC LIMIT 1",
                     (PROFILE_KIND,))
        if not r:
            return None
        r = dict(r[0])
        return {"id": r["id"], "version": json.loads(r["meta"] or "{}").get("version", 1), "body": r["content"],
                "agent": self.s.label(r["agent_id"])[1] if r["agent_id"] else "", "updated": iso(r["created_at"]),
                "ts": r["created_at"]}

    def _version(self) -> int:
        p = self.latest()
        return int(p["version"]) if p else 0

    def rules(self, surface: str = "", project: str | None = None) -> list[dict]:
        rows = self.prefs.get(project=project, include_all=not project, max_tokens=100000)["preferences"]
        byid = {r["id"]: r["category"] for r in self.s.q("SELECT id, category FROM preferences WHERE category LIKE 'design:%'")}
        out = []
        for p in rows:
            if p["id"] not in byid:
                continue
            sec, surf = _split(byid[p["id"]])
            if surface and surf and surf != surface.lower():
                continue
            out.append(p | {"section": sec, "surface": surf})
        order = list(SECTIONS)
        out.sort(key=lambda p: (order.index(p["section"]) if p["section"] in order else 99, -p["confidence"]))
        return out

    def recent_feedback(self, since: float = 0, limit: int = 20) -> list[dict]:
        rows = self.s.q("SELECT id, content, project_id, agent_id, created_at FROM items WHERE kind='observation' "
                        "AND tags LIKE ? AND created_at>? ORDER BY created_at DESC LIMIT ?",
                        (f'%"{FEEDBACK_TAG}"%', since, limit))
        return [{"id": r["id"], "text": r["content"], "project": self.s.project_name(r["project_id"]),
                 "agent": self.s.label(r["agent_id"])[1] if r["agent_id"] else "", "at": iso(r["created_at"])}
                for r in rows]

    def profile(self, surface: str = "", project: str | None = None, max_tokens: int = 2500) -> dict:
        p = self.latest()
        rules = self.rules(surface, project)
        fresh = self.recent_feedback(p["ts"] if p else 0, 12)
        parts = []
        if rules:
            parts.append("DESIGN RULES (live; confidence decays unless reinforced):")
            last = None
            for r in rules:
                if r["section"] != last:
                    parts.append(f"{SECTIONS.get(r['section'], r['section'])}:")
                    last = r["section"]
                parts.append(f"- {r['statement']}" + (f" [{r['surface']}]" if r["surface"] else "")
                             + f" ({r['confidence']:.2f}, {r['id']})")
        if fresh:
            parts.append("NEW FEEDBACK since the profile was written (the user's own words, newest first; this "
                         "outranks the profile where they disagree):")
            parts += [f"- [{f['at'][:10]}{', ' + f['project'] if f['project'] else ''}] {f['text'][:300]}" for f in fresh]
        head = "\n".join(parts)
        body = ""
        if p:
            budget = max(200, max_tokens - estimate_tokens(head) - 40)
            body = p["body"]
            while estimate_tokens(body) > budget and "\n" in body:
                body = body.rsplit("\n", 1)[0]
            body = f"DESIGN IDEOLOGY v{p['version']} (updated {p['updated'][:10]}{' by ' + p['agent'] if p['agent'] else ''}):\n{body}"
        text = "\n\n".join(x for x in (head, body) if x) or (
            "No design ideology yet. Read the user's feedback with design_evidence() and write one with "
            "design_profile_set(), and record reactions with design_feedback().")
        return {"text": text, "profile": p, "rules": rules, "feedback": fresh}

    def pointer(self) -> str:
        """One line for pickup, only when there is something to read."""
        p = self.latest()
        n = self.s.q("SELECT COUNT(*) c FROM preferences WHERE category LIKE 'design:%' AND status='active'")[0]["c"]
        if not p and not n:
            return ""
        return ("DESIGN: before any visual or UI work call design_profile(surface) "
                + (f"(ideology v{p['version']}, {p['updated'][:10]}; " if p else "(")
                + f"{n} rules); record the user's reactions with design_feedback().")

    def evidence(self, limit: int = 400, max_chars: int = 200000) -> dict:
        """Everything Commons holds about the user's design taste, for whoever (re)writes the profile."""
        rows = self.s.q("SELECT content, project_id, created_at FROM items WHERE tags LIKE '%\"user\"%' "
                        "AND tags LIKE '%\"conversation\"%' ORDER BY created_at DESC LIMIT 20000")
        said = []
        for r in rows:
            if is_design_feedback(r["content"]):
                said.append(f"[{iso(r['created_at'])[:10]}] ({self.s.project_name(r['project_id']) or '-'}) "
                            f"{' '.join(r['content'].split())[:600]}")
            if len(said) >= limit:
                break
        said += [f"[{f['at'][:10]}] ({f['project'] or '-'}) {f['text'][:600]}" for f in self.recent_feedback(0, limit)]
        said = sorted(set(said))
        rules = [f"- [{r['section']}{'@' + r['surface'] if r['surface'] else ''}] {r['statement']}" for r in self.rules()]
        notes = self.s.q("SELECT content FROM items WHERE kind IN ('memory','decision') AND superseded_by IS NULL AND "
                         "tags NOT LIKE '%\"conversation\"%' AND (content LIKE '%design%' OR content LIKE '%owner said%') "
                         "AND (content LIKE '%reject%' OR content LIKE '%liked%' OR content LIKE '%owner%' OR "
                         "content LIKE '%user said%') ORDER BY created_at DESC LIMIT 120")
        text = ("USER'S OWN WORDS ABOUT DESIGN:\n" + "\n".join(said) + "\n\nRECORDED DESIGN RULES:\n" + "\n".join(rules)
                + "\n\nAGENT NOTES:\n" + "\n---\n".join(n["content"][:1500] for n in notes))
        return {"text": text[:max_chars], "said": len(said), "rules": len(rules), "notes": len(notes)}

    def rebuild(self, agent_id: str | None = None) -> dict:
        """Re-distill the profile on the design_distiller role's model; without one, hand the job to the caller."""
        ev = self.evidence()
        prev = self.latest()
        prompt = (DISTILL_PROMPT + ("\n\nCURRENT PROFILE (revise it, keep what still holds):\n" + prev["body"]
                                    if prev else "") + "\n\nEVIDENCE:\n" + ev["text"])
        from .providers import ProviderLayer
        out = ProviderLayer(self.s).call_role("design_distiller", prompt, max_tokens=6000)
        if out and len(out.strip()) > 200:
            return self.profile_set(out, agent_id=agent_id, sources=f"rebuild: {ev['said']} quotes, {ev['rules']} rules")
        return {"needs_agent": True, "do": "No design_distiller model is configured. Read design_evidence(), write the "
                "profile following its instructions, then design_profile_set(body).", "evidence_size": len(ev["text"])}


DISTILL_PROMPT = """Write the user's DESIGN IDEOLOGY: the profile every AI agent reads before visual or UI work, so the
user gets a consistent style they like. Use only the evidence; cite a date and a short verbatim quote for each claim.
Sections, in order:
1. Core in one paragraph (what every design for this user must be).
2. Hard no (rejected more than once or stated as absolute).
3. Strong preferences.
4. By surface: marketing pages, dashboards and status pages, apps and tools, emails, phone.
5. Liked examples (what was approved or picked) and rejected examples (what was wrong with each).
6. What the user's words mean ("generic", "no taste", "too simple", "unreadable", ...) and what fixed it.
7. Changes over time and contradictions, with which position is current (newer and repeated wins).
8. Process rules: how to check a design before showing it.
9. A pre-flight checklist of at most 15 lines.
Plain markdown, short lines, no em dashes."""
