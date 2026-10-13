"""Agents' own tools (agy, grok, claude): Commons knows each one's version and keeps it up to date.

The machine an agent runs on reports the agent's CLI version with every `commons quota sync` (quota_report cli=...).
The hub answers whether to update now: with agent_updates=auto (the default) when a newer version is known, or once a
day for a CLI that can only find out by trying (`agy update`, `claude update`); and whenever the owner pressed Update
now. That machine runs the agent's own updater and reports back; the owner gets a notification when a version changes
or an update fails.
"""
from __future__ import annotations

import json

from .ids import iso, now
from .store import Store

DAY = 86400


def _get(s: Store, agent_id: str) -> dict:
    r = s.q("SELECT value FROM meta WHERE key=?", (f"agent_cli:{agent_id}",))
    return json.loads(r[0]["value"]) if r else {}


def _put(s: Store, agent_id: str, rec: dict) -> None:
    s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"agent_cli:{agent_id}", json.dumps(rec)))


def report(s: Store, agent_id: str, cli: dict) -> dict:
    """Record what the agent's machine says; returns {"update": bool} telling it whether to run the updater now."""
    from . import settings
    from .escalation import notify
    rec, name = _get(s, agent_id), s.label(agent_id)[1]
    old = rec.get("version")
    rec.update(version=cli.get("version") or old, seen_at=now(), can_check=bool(cli.get("can_check")))
    if cli.get("latest"):
        rec["latest"] = cli["latest"]
    if "result" in cli:  # the updater just ran
        rec.update(updated_at=now(), requested=False, ok=bool(cli.get("ok")), last_result=str(cli["result"])[-300:])
        if not cli.get("ok"):
            notify(s, f"{name}: updating its tool failed", rec["last_result"], level="warn")
        elif old and rec["version"] != old:
            notify(s, f"{name} updated {old} → {rec['version']}", "Its model list was refreshed too.", level="info")
        if rec.get("latest") and cli.get("ok"):
            rec["latest"] = rec["version"]
    mode = settings.get(s, "agent_updates")
    newer = bool(rec.get("latest") and rec.get("version") and rec["latest"] != rec["version"])
    if mode == "notify" and newer and rec.get("told") != rec["latest"]:
        notify(s, f"{name} {rec['latest']} is out", f"{name} runs {rec['version']}. Update it from the Models page.",
               level="info")
        rec["told"] = rec["latest"]
    update = "result" not in cli and (bool(rec.get("requested")) or mode == "auto" and (
        newer or (not rec["can_check"] and now() - rec.get("updated_at", 0) > DAY)))
    _put(s, agent_id, rec)
    return {"update": update}


def request(s: Store, agent: str) -> dict:
    """The owner pressed Update now: the agent's machine runs its updater on its next sync (within 10 minutes)."""
    a = s.get_agent(agent)
    if not a:
        from .store import CommonsError
        raise CommonsError(f"unknown agent {agent}")
    rec = _get(s, a["id"])
    rec["requested"] = True
    _put(s, a["id"], rec)
    return {"agent": agent, "requested": True}


def listing(s: Store) -> list[dict]:
    out = []
    for r in s.q("SELECT key, value FROM meta WHERE key LIKE 'agent_cli:%'"):
        rec = json.loads(r["value"])
        out.append({"agent": s.label(r["key"].split(":", 1)[1])[1], "version": rec.get("version"),
                    "latest": rec.get("latest"), "update_available": bool(rec.get("latest") and rec.get("version")
                                                                         and rec["latest"] != rec["version"]),
                    "updated_at": iso(rec["updated_at"]) if rec.get("updated_at") else None, "ok": rec.get("ok"),
                    "last_result": rec.get("last_result"), "requested": bool(rec.get("requested")),
                    "seen_at": iso(rec["seen_at"]) if rec.get("seen_at") else None})
    return sorted(out, key=lambda x: x["agent"])
