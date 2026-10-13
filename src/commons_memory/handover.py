"""Hand-over: every agent gives Commons everything it has, and keeps it current, so Commons is always complete.

handover_status() lists what Commons still lacks from the calling agent, with the exact tool for each item: its profile
(expertise, what it can reach), its memories, the keys and passwords it holds, its scheduled tasks, its usage windows,
its current work, and live activity (hooks, or turn() in chat apps). An item counts as handed over when Commons sees
it (rows from that agent), or when the agent declares it has nothing for it (handover_report(item, "none")).
pickup() names the missing items until the hand-over is complete.
"""
from __future__ import annotations

import json

from .ids import iso, now
from .store import CommonsError, Store

ITEMS = {
    "profile": "set_profile(expertise={task_type: 0..1}, strengths=[...], reach=[what you can access: networks, hosts, "
               "SSH, CLIs, apps])",
    "memories": "remember / import_items with everything you know about the user, their projects and systems (your "
                "own memory files, notes, past conversations)",
    "credentials": "vault_deposit(name, value, service, base_urls, auth) for every API key, token and password you "
                   "hold; it is encrypted and only ever used through the decision agent",
    "schedules": "schedule_report for every task you run on a schedule (cron, how it runs, the acceptance bar)",
    "quota": "quota_report with your usage windows (5-hour, weekly), if your client has limits",
    "work": "start_work for anything you are working on now",
    "skills": "skill_publish every reusable skill you have (every AI then sees it), and list the ones tied to your "
              "environment with set_profile(skills=[...]) so others can ask you via skill_help",
    "activity": "keep Commons current while you work: `commons hooks install <your CLI>` on your machine, or call "
                "turn(user_message, reply) every turn in a chat app",
}


def _declared(s: Store, agent_id: str) -> dict:
    return {r["key"].rsplit(":", 1)[1]: json.loads(r["value"])
            for r in s.q("SELECT key, value FROM meta WHERE key LIKE ?", (f"handover:{agent_id}:%",))}


def _have(s: Store, agent_id: str) -> dict:
    a = s.get_agent(agent_id) or {}
    prof = json.loads(a.get("profile") or "{}")
    one = lambda sql, *args: s.q(sql, args)[0]["n"]  # noqa: E731
    return {
        "profile": bool(prof.get("expertise")) and bool(prof.get("reach")),
        "memories": one("SELECT COUNT(*) n FROM items WHERE agent_id=? AND kind IN ('memory','fact','decision')", agent_id),
        "credentials": one("SELECT COUNT(*) n FROM vault_creds WHERE deposited_by=?", agent_id),
        "schedules": one("SELECT COUNT(*) n FROM recurring WHERE owner_agent=?", agent_id),
        "quota": one("SELECT COUNT(*) n FROM agent_quota WHERE agent_id=?", agent_id),
        "work": one("SELECT COUNT(*) n FROM work_claims WHERE agent_id=?", agent_id),
        "activity": one("SELECT COUNT(*) n FROM observations WHERE agent_id=? AND ts>?", agent_id, now() - 86400),
        "skills": one("SELECT COUNT(*) n FROM skills WHERE author_agent=?", agent_id) or len(prof.get("skills") or []),
    }


def status(s: Store, agent_id: str) -> dict:
    have, dec = _have(s, agent_id), _declared(s, agent_id)
    done, missing = {}, []
    for item, how in ITEMS.items():
        if have[item] or item in dec:
            done[item] = have[item] if have[item] else f"declared {dec[item]['status']}: {dec[item].get('note', '')}".strip()
        else:
            missing.append({"item": item, "do": how})
    return {"complete": not missing, "handed_over": done, "missing": missing,
            "note": "Hand over anything new as soon as you have it; Commons should never be missing what you know."}


def report(s: Store, agent_id: str, item: str, status_: str = "none", note: str = "") -> dict:
    if item not in ITEMS:
        raise CommonsError(f"item is one of {', '.join(ITEMS)}")
    if status_ not in ("none", "done"):
        raise CommonsError("status is none (you have nothing for it) or done")
    s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"handover:{agent_id}:{item}",
                                                    json.dumps({"status": status_, "note": note[:300], "at": iso(now())})))
    return status(s, agent_id)


def nudge(s: Store, agent_id: str | None) -> str:
    """One line for pickup(): what Commons still needs from this agent ('' when complete)."""
    if not agent_id:
        return ""
    try:
        st = status(s, agent_id)
    except Exception:  # noqa: BLE001 - never break a check-in over this
        return ""
    if st["complete"]:
        return ""
    return ("HAND-OVER INCOMPLETE: Commons still needs your " + ", ".join(m["item"] for m in st["missing"])
            + ". Call handover_status() for how.\n\n")
