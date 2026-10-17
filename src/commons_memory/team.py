"""Who is on the team. The hub writes each agent's card from what it can check (who issued its key and when, where it
runs, how it is woken, its usage headroom, the schedules it owns, the work it actually finished) and keeps what an agent
says about itself apart, marked as claimed. Agents meet a newcomer at their next pickup(), never through a broadcast:
a broadcast wakes every daemon (and spends every agent's quota) to deliver a claim nobody can check."""
from __future__ import annotations

import json
import re

from .ids import iso, now
from .store import CommonsError, Store

HIDDEN = {"user", "cli", "commons", "dashboard-user", "librarian"}  # the hub's own identities, not teammates


def members(s: Store) -> list[dict]:
    """Agents with their own key that have done something (or checked in this week): test and placeholder names drop out."""
    t = now()
    out = []
    for a in s.q("SELECT * FROM agents WHERE key_hash IS NOT NULL ORDER BY created_at"):
        if a["name"] in HIDDEN or a["name"].startswith("provider:"):
            continue
        if ((a["last_seen"] or 0) > t - 3 * 86400 or s.q("SELECT 1 FROM items WHERE agent_id=? LIMIT 1", (a["id"],))
                or s.q("SELECT 1 FROM subtasks WHERE claimed_by=? LIMIT 1", (a["id"],))):
            out.append(dict(a))
    return out


def shown(a: dict) -> str:
    return a.get("display") or a["name"]


def _key(s: Store, a: dict) -> dict:
    """Who issued the agent's key and when, from the audit trail (older keys predate recording the issuer)."""
    for r in s.q("SELECT action, detail, ts FROM audit WHERE target=? AND action IN ('agent.register','agent.key_created') "
                 "ORDER BY ts DESC", (a["id"],)):
        d = json.loads(r["detail"] or "{}") if (r["detail"] or "").startswith("{") else {}
        if r["action"] == "agent.key_created" or d.get("key"):
            by = d.get("by") or ("the dashboard's Connect page" if d.get("via") == "connect" else "")
            return {"own_key": True, "issued_at": iso(r["ts"]), "issued_by": by or "not recorded"}
    return {"own_key": True, "issued_at": iso(a["created_at"]) if a.get("created_at") else None,
            "issued_by": "issued before Commons recorded who issues keys"}


def card(s: Store, name: str) -> dict:
    from . import quota as Q
    from .messaging import presence
    a = s.get_agent(name)
    if not a or a["id"] not in {m["id"] for m in members(s)}:
        raise CommonsError(f"no teammate called {name!r}; who() lists the team")
    prof = json.loads(a.get("profile") or "{}")
    ok, until, blocking = Q.can_take(s, a["id"])
    wins = next((x["windows"] for x in Q.status(s) if x["agent"] == a["name"]), [])
    p = presence(s, a["id"])
    learned = {}
    for r in s.q("SELECT task_type, COUNT(*) n, SUM(success) ok FROM outcomes WHERE agent_id=? GROUP BY task_type", (a["id"],)):
        learned[r["task_type"]] = {"finished": r["n"], "succeeded": int(r["ok"] or 0)}
    recent = [r["title"] for r in s.q("SELECT title FROM subtasks WHERE claimed_by=? AND status='done' ORDER BY updated_at DESC "
                                      "LIMIT 3", (a["id"],))]
    return {
        "name": a["name"], "display": shown(a), "what": a.get("platform") or a.get("vendor") or "",
        "model": a.get("model") or "", "joined": iso(a["created_at"]) if a.get("created_at") else None,
        "key": _key(s, a),
        "presence": {"state": p.get("state"), "woken_by": p.get("wake_mode"), "last_seen": iso(a["last_seen"]) if a["last_seen"] else None},
        "headroom": {"can_take_work": ok, "waits_until": iso(until) if until else None, "blocking": blocking,
                     "windows": [{k: w.get(k) for k in ("label", "used_pct", "resets_at", "runs_out_at")} for w in wins]},
        "schedules": [r["name"] for r in s.q("SELECT name FROM recurring WHERE owner_agent=? AND enabled=1 ORDER BY name", (a["id"],))],
        "measured": {"by_type": learned, "recent": recent},
        "claimed": {k: v for k, v in {"about": a.get("description") or "", "expertise": prof.get("expertise") or {},
                                      "strengths": prof.get("strengths") or [], "reach": prof.get("reach") or []}.items() if v},
        "note": "Checked by the hub: name, display name (set by the user), key, joined, presence, headroom, schedules and "
                "measured work. 'claimed' is what the agent says about itself.",
    }


def line(s: Store, a: dict) -> str:
    """One line per teammate for pickup and who(): what it is, when it joined and who let it in."""
    k = _key(s, a)
    prof = json.loads(a.get("profile") or "{}")
    good = sorted((prof.get("expertise") or {}).items(), key=lambda kv: -float(kv[1] or 0))[:3]
    what = (a.get("platform") or a.get("vendor") or "").strip()
    bits = [f"{shown(a)}" + (f" (`{a['name']}`)" if shown(a) != a["name"] else ""),
            what[:80] if what and what != shown(a) else "",
            f"joined {iso(a['created_at'])[:10]}" if a.get("created_at") else "",
            f"key issued by {k['issued_by']}" if k["issued_by"] not in ("not recorded",) and "before" not in k["issued_by"] else "",
            ("good at " + ", ".join(t for t, _ in good) + " (claimed)") if good else ""]
    return "- " + ", ".join(b for b in bits if b)


def roster(s: Store) -> list[dict]:
    return [{"name": a["name"], "display": shown(a), "line": line(s, a)[2:]} for a in members(s)]


def nudge(s: Store, agent_id: str | None) -> str:
    """pickup(): the whole team on an agent's first check-in, then only the teammates it hasn't met yet (whenever they
    joined or first connected)."""
    if not agent_id:
        return ""
    key = f"team_met:{agent_id}"
    r = s.q("SELECT value FROM meta WHERE key=?", (key,))
    met = set(json.loads(r[0]["value"])) if r else None
    team = [a for a in members(s) if a["id"] != agent_id]
    s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(sorted((met or set()) | {a["id"] for a in team}))))
    if met is None:
        return ("YOUR TEAM (from Commons, checked by the hub; who(name) for anyone's card):\n"
                + "\n".join(line(s, a) for a in team) + "\n\n") if team else ""
    new = [a for a in team if a["id"] not in met]
    return ("NEW ON THE TEAM since you last checked in (from Commons; who(name) for the card):\n"
            + "\n".join(line(s, a) for a in new) + "\n\n") if new else ""


def taken(s: Store, label: str, except_id: str | None = None) -> bool:
    """Is this label already someone's: a hub identity, or another agent's handle or display name (any case or script)?"""
    k = label.strip().casefold()
    if k in HIDDEN or k.startswith("provider:"):
        return True
    return any(k in (str(r["name"]).casefold(), str(r["display"] or "").casefold())
               for r in s.q("SELECT name, display FROM agents WHERE id<>?", (except_id or "",)))


def set_display(s: Store, name: str, display: str) -> dict:
    """The user names an agent for people ("Grok bot (Botty)"); its handle stays, so messages to it keep arriving.
    A display name can't be another agent's handle or name, or the hub's, so nobody can pass for someone else."""
    a = s.get_agent(name)
    if not a:
        raise CommonsError(f"unknown agent {name!r}")
    display = re.sub(r"\s+", " ", display or "")[:60].strip()
    if display and taken(s, display, a["id"]):
        raise CommonsError(f"{display!r} is another agent's name (or the hub's): pick one nobody else uses")
    s.x("UPDATE agents SET display=? WHERE id=?", (display or None, a["id"]))
    s.audit(None, "agent.display", a["id"], {"display": display})
    return {"name": a["name"], "display": display or a["name"]}


def introduce(s: Store, a_name: str, b_name: str, why: str) -> dict:
    """The user says two agents work together: each gets one note about the other, from the hub. Never a broadcast."""
    from .messaging import Messaging
    a, b = s.get_agent(a_name), s.get_agent(b_name)
    if not a or not b or a["id"] == b["id"]:
        raise CommonsError("introduce two different agents by name")
    why = (why or "").strip()
    if len(why) < 8:
        raise CommonsError("say why they should know each other (what they work on together)")
    sent = []
    for me, other in ((a, b), (b, a)):
        text = (f"The user introduces you to {shown(other)} (`{other['name']}`): {why}\n\n{line(s, other)[2:]}\n\n"
                f"who('{other['name']}') shows its card; msg_send(to='{other['name']}') reaches it.")
        sent.append(Messaging(s).send(None, me["name"], text, mtype="fyi", subject=f"Meet {shown(other)}",
                                      by="the user's introduction", wake=False)["id"])
    s.audit(None, "agent.introduce", f"{a['id']}+{b['id']}", {"why": why[:300]})
    return {"introduced": [a["name"], b["name"]], "messages": sent}
