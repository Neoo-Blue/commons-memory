"""Quota windows per agent (5-hour, weekly, ...) and the user's reserve.

Agents (or `commons quota sync`, which reads the aiusage database) report each window's used % and reset time. Commons
logs every report, so it learns two things about the user without being told:
- when the user personally works with each agent (snapshots that rise while Commons had no work on that agent), and
- how fast the user burns each window during those hours.
From that it keeps a reserve: the larger of a static floor (COMMONS_QUOTA_RESERVE_5H / _WEEKLY, % of the window) and
the user's expected need until the window resets. An agent takes Commons work only while
    100 - used - reserve >= the task type's learned cost on that agent
and otherwise the work waits for the earliest reset (orchestrator.allocate) and resumes from its checkpoint.
"""
from __future__ import annotations

import json
import os
import re
import time
from statistics import median

from .ids import iso, now
from .store import CommonsError, Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_quota(agent_id TEXT, win TEXT, period TEXT, used_pct REAL, resets_at REAL,
  source TEXT, updated_at REAL, PRIMARY KEY(agent_id, win));
CREATE TABLE IF NOT EXISTS quota_log(agent_id TEXT, win TEXT, used_pct REAL, ts REAL, busy INTEGER);
CREATE INDEX IF NOT EXISTS quota_log_aw ON quota_log(agent_id, win, ts);
CREATE TABLE IF NOT EXISTS quota_cost(agent_id TEXT, task_type TEXT, win TEXT, delta REAL, ts REAL);
"""
HOURS = {"5h": 5, "daily": 24, "weekly": 168, "monthly": 720}


def migrate(db) -> None:
    db.executescript(SCHEMA)
    from .facts import add_columns
    add_columns(db, [("subtasks", "checkpoint", "TEXT"), ("subtasks", "not_before", "REAL")])


def period_of(window: str) -> str:
    w = window.lower()
    if re.search(r"five_hour|5h|5-hour|session", w):
        return "5h"
    if re.search(r"seven_day|week|grok", w) or w.startswith("product:"):
        return "weekly"
    if re.search(r"day|daily", w):
        return "daily"
    if re.search(r"month", w):
        return "monthly"
    return "other"


def _busy(s: Store, agent_id: str, t: float) -> bool:
    """Was Commons itself driving this agent around t (claimed work, or work finished in the last 15 minutes)?"""
    return bool(s.q("SELECT 1 FROM subtasks WHERE claimed_by=? AND (status='claimed' OR (updated_at>? AND updated_at<=?)) "
                    "LIMIT 1", (agent_id, t - 900, t + 60)))


def report(s: Store, agent_id: str, windows: list[dict], source: str = "agent") -> dict:
    """windows: [{window, used_pct (0-100), resets_at (epoch or ISO), period?}]."""
    from .store import _parse_time
    t, out = now(), []
    busy = int(_busy(s, agent_id, t))
    for w in windows or []:
        name = str(w.get("window") or "").strip()
        if not name:
            raise CommonsError("each window needs a name (e.g. five_hour, weekly)")
        used = float(w.get("used_pct") if w.get("used_pct") is not None else w.get("used", 0))
        if not 0 <= used <= 100:
            raise CommonsError("used_pct is a percentage 0-100")
        ra = w.get("resets_at")
        ra = float(ra) if isinstance(ra, (int, float)) else (_parse_time(ra) if ra else None)
        per = w.get("period") or period_of(name)
        s.x("INSERT INTO agent_quota(agent_id,win,period,used_pct,resets_at,source,updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(agent_id,win) DO UPDATE SET period=excluded.period, used_pct=excluded.used_pct, "
            "resets_at=excluded.resets_at, source=excluded.source, updated_at=excluded.updated_at",
            (agent_id, name, per, used, ra, source, t))
        s.x("INSERT INTO quota_log(agent_id,win,used_pct,ts,busy) VALUES(?,?,?,?,?)", (agent_id, name, used, t, busy))
        out.append(name)
    return {"agent_id": agent_id, "windows": out}


def windows(s: Store, agent_id: str) -> list[dict]:
    """Current windows; one whose reset time has passed counts as fresh (0% used) until the next report."""
    t = now()
    out = []
    for r in s.q("SELECT *, win AS window FROM agent_quota WHERE agent_id=?", (agent_id,)):
        d = dict(r)
        if d["resets_at"] and d["resets_at"] <= t:
            d["used_pct"], d["stale_reset"] = 0.0, True
        out.append(d)
    return out


def _activity(s: Store, agent_id: str, window: str, days: float = 28) -> tuple[float, dict[int, float]]:
    """(user burn in % per active hour, P(user active) per hour-of-week) from snapshots taken while Commons was idle."""
    rows = s.q("SELECT used_pct, ts, busy FROM quota_log WHERE agent_id=? AND win=? AND ts>? ORDER BY ts",
               (agent_id, window, now() - days * 86400))
    rates, seen, active = [], {}, {}
    for a, b in zip(rows, rows[1:]):
        dt = (b["ts"] - a["ts"]) / 3600
        if dt <= 0 or dt > 6 or b["busy"] or a["busy"]:
            continue
        how = int(time.gmtime(a["ts"]).tm_wday * 24 + time.gmtime(a["ts"]).tm_hour)
        seen[how] = seen.get(how, 0) + 1
        d = b["used_pct"] - a["used_pct"]
        if d > 0:  # a drop is a reset, not negative use
            rates.append(d / dt)
            active[how] = active.get(how, 0) + 1
    prob = {h: active.get(h, 0) / n for h, n in seen.items()}
    return (median(rates) if rates else 0.0), prob


def user_need(s: Store, agent_id: str, window: str, resets_at: float | None) -> float:
    """% of the window the user is expected to need before it resets, from their own past use."""
    if not resets_at:
        return 0.0
    rate, prob = _activity(s, agent_id, window)
    if not rate:
        return 0.0
    t, hours = now(), 0.0
    while t < resets_at and hours < 24 * 8:
        g = time.gmtime(t)
        hours += prob.get(g.tm_wday * 24 + g.tm_hour, 0.0)
        t += 3600
    return min(90.0, rate * hours)


def reserve(s: Store, agent_id: str, w: dict) -> float:
    a = s.q("SELECT profile FROM agents WHERE id=?", (agent_id,))
    prof = json.loads((a[0]["profile"] if a else None) or "{}")
    floor = (prof.get("limits") or {}).get("reserve", {}).get(w["period"])
    if floor is None:
        from . import settings
        floor = float(os.environ.get(f"COMMONS_QUOTA_RESERVE_{w['period'].upper()}") or settings.get(s, "reserve_pct"))
    return max(float(floor), user_need(s, agent_id, w["window"], w.get("resets_at")))


def task_cost(s: Store, agent_id: str, task_type: str, window: str) -> float:
    rows = [r["delta"] for r in s.q("SELECT delta FROM quota_cost WHERE agent_id=? AND task_type=? AND win=? "
                                    "ORDER BY ts DESC LIMIT 10", (agent_id, task_type, window))]
    return median(rows) if rows else float(os.environ.get("COMMONS_QUOTA_TASK_PCT", "5"))


def can_take(s: Store, agent_id: str, task_type: str = "general") -> tuple[bool, float | None, str | None]:
    """(has headroom for one more task of this type, when the blocking window resets, which window blocks).
    No reported windows = unknown = allowed (an agent that never reports is limited only by its own errors)."""
    worst = None
    for w in windows(s, agent_id):
        room = 100 - w["used_pct"] - reserve(s, agent_id, w) - task_cost(s, agent_id, task_type, w["window"])
        if room < 0 and (worst is None or (w["resets_at"] or 0) > (worst["resets_at"] or 0)):
            worst = w  # the latest-resetting blocked window is the one that decides when work can resume
    if worst is None:
        return True, None, None
    return False, worst["resets_at"] or now() + 3600, worst["window"]


def headroom(s: Store, agent_id: str, task_type: str = "general") -> tuple[float | None, bool]:
    """(room left after the user's reserve and one task of this type, 0..1, the tightest window deciding; whether that
    room expires unused soon: a window with real room resets within 3 hours). None = the agent reports no windows."""
    ws = windows(s, agent_id)
    if not ws:
        return None, False
    rooms = [(100 - w["used_pct"] - reserve(s, agent_id, w) - task_cost(s, agent_id, task_type, w["window"]), w)
             for w in ws]
    room = max(0.0, min(r for r, _ in rooms)) / 100
    expiring = room >= 0.2 and any(r >= 30 and w["resets_at"] and 0 < w["resets_at"] - now() < 3 * 3600 for r, w in rooms)
    return room, expiring


def snapshot(s: Store, agent_id: str) -> dict:
    ws = windows(s, agent_id)
    return {"ts": now(), "w": {w["window"]: w["used_pct"] for w in ws}} if ws else {}


def record_cost(s: Store, agent_id: str, task_type: str, before: dict) -> None:
    """Learned per-task cost: how much of each window one completed subtask of this type used. Only windows that were
    re-reported while the task ran count (an unchanged stale reading would teach a cost of zero)."""
    if not before:
        return
    for w in windows(s, agent_id):
        b = before.get("w", {}).get(w["window"])
        if b is not None and w["updated_at"] > before.get("ts", 0) and not w.get("stale_reset") and w["used_pct"] >= b:
            s.x("INSERT INTO quota_cost(agent_id,task_type,win,delta,ts) VALUES(?,?,?,?,?)",
                (agent_id, task_type, w["window"], round(w["used_pct"] - b, 3), now()))


def status(s: Store) -> list[dict]:
    out = []
    for a in s.q("SELECT DISTINCT q.agent_id, a.name FROM agent_quota q JOIN agents a ON a.id=q.agent_id ORDER BY a.name"):
        ws = []
        for w in windows(s, a["agent_id"]):
            res = reserve(s, a["agent_id"], w)
            ws.append({"window": w["window"], "period": w["period"], "used_pct": round(w["used_pct"], 1),
                       "reserve_pct": round(res, 1), "free_for_commons_pct": round(max(0.0, 100 - w["used_pct"] - res), 1),
                       "resets_at": iso(w["resets_at"]) if w["resets_at"] else None, "source": w["source"],
                       "updated": iso(w["updated_at"])})
        ok, until, win = can_take(s, a["agent_id"])
        out.append({"agent": a["name"], "can_take_work": ok, "waits_until": iso(until) if until else None,
                    "blocking_window": win, "windows": ws})
    return out


# aiusage provider/window -> Commons agents (override with ~/.commons/quota-map.json)
DEFAULT_MAP = {
    "claude": {"five_hour": ["my-agent"], "seven_day": ["my-agent"]},
    "antigravity": {"gemini_models:5h": ["antigravity"], "gemini_models:weekly": ["antigravity"]},
    "grok": {"product:grokbuild": ["grok-build"], "weekly_all": ["grok-build", "grok"], "product:grokchat": ["grok"]},
}


# Commons agent -> the local command that lists the models it can run right now (override: "_models" in quota-map.json)
MODEL_CMDS = {"antigravity": ["agy", "models"], "grok-build": ["grok", "models"]}


def local_models(cmds: dict | None = None) -> dict[str, list[dict]]:
    """Each agent's live model catalog from its own CLI: `agy models` (slug<TAB>name) or `grok models` (bullets).
    An agent whose CLI is missing or fails is left out, so the hub keeps what it last knew."""
    import os
    import re
    import shutil
    import subprocess
    path = os.environ.get("PATH", "") + os.pathsep + os.path.expanduser("~/.local/bin")  # launchd's PATH is bare
    out = {}
    for agent, cmd in (cmds or MODEL_CMDS).items():
        exe = shutil.which(cmd[0], path=path)
        if not exe:
            continue
        try:
            r = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        ms = []
        for line in r.stdout.splitlines():
            if "\t" in line:
                mid, name = line.split("\t", 1)
                ms.append({"id": mid.strip(), "name": name.strip()})
            elif m := re.match(r"\s*[*-]\s+(\S+)", line):
                ms.append({"id": m.group(1), "name": m.group(1)})
        if r.returncode == 0 and ms:
            out[agent] = ms
    return out


def from_aiusage(db_path: str | None = None, mapping: dict | None = None) -> dict[str, list[dict]]:
    """Latest window per provider from the aiusage database, grouped by Commons agent name."""
    import sqlite3
    from pathlib import Path
    p = Path(db_path or Path.home() / ".aiusage" / "usage.db")
    if not p.exists():
        raise CommonsError(f"no aiusage database at {p}")
    if mapping is None:
        mp = Path.home() / ".commons" / "quota-map.json"
        mapping = json.loads(mp.read_text()) if mp.exists() else DEFAULT_MAP
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    out: dict[str, list[dict]] = {}
    for prov, win, used, ra in db.execute(
            "SELECT q.provider, q.window, q.used_pct, q.resets_at FROM quota q JOIN (SELECT provider, window, MAX(ts) m "
            "FROM quota GROUP BY provider, window) l ON l.provider=q.provider AND l.window=q.window AND l.m=q.ts"):
        for agent in (mapping.get(prov) or {}).get(win, []):
            if used is not None:
                out.setdefault(agent, []).append({"window": f"{prov}:{win}", "used_pct": float(used), "resets_at": ra,
                                                  "period": period_of(win)})
    return out
