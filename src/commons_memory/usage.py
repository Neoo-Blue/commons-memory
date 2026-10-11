"""Usage & cost tracking: tokens in/out, $ estimate from a configurable price table, time. Plus spend caps."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .ids import now
from .store import CommonsError, Store

# USD per 1M tokens (input, output). Override/extend with COMMONS_PRICES=/path/prices.json or ~/.commons/prices.json
DEFAULT_PRICES = {
    "claude-opus": (15.0, 75.0), "claude-sonnet": (3.0, 15.0), "claude-haiku": (0.8, 4.0),
    "gpt-5": (1.25, 10.0), "gpt-4o": (2.5, 10.0), "gpt-4o-mini": (0.15, 0.6), "o3": (2.0, 8.0),
    "grok-4": (3.0, 15.0), "grok-3-mini": (0.3, 0.5), "gemini-2.5-pro": (1.25, 10.0), "gemini-2.5-flash": (0.3, 2.5),
    "default": (1.0, 4.0), "local": (0.0, 0.0), "mock": (0.0, 0.0),
}


def prices() -> dict:
    p = dict(DEFAULT_PRICES)
    path = os.environ.get("COMMONS_PRICES") or str(Path.home() / ".commons" / "prices.json")
    if os.path.exists(path):
        p.update({k: tuple(v) for k, v in json.load(open(path)).items()})
    return p


def estimate_cost(model: str | None, tokens_in: int, tokens_out: int) -> float:
    table = prices()
    m = (model or "default").lower()
    key = m if m in table else next((k for k in sorted(table, key=len, reverse=True) if m.startswith(k)), "default")
    pin, pout = table[key]
    return round((tokens_in * pin + tokens_out * pout) / 1e6, 6)


def daily_cap() -> float | None:
    v = os.environ.get("COMMONS_DAILY_COST_CAP")
    return float(v) if v else None


class Usage:
    def __init__(self, store: Store):
        self.s = store

    def report(self, agent_id: str | None, *, tokens_in: int = 0, tokens_out: int = 0, model: str | None = None,
               cost: float | None = None, seconds: float = 0.0, project: str | None = None, job_id: str | None = None,
               subtask_id: str | None = None, note: str = "") -> dict:
        if tokens_in < 0 or tokens_out < 0:
            raise CommonsError("token counts must be >= 0")
        c = cost if cost is not None else estimate_cost(model, tokens_in, tokens_out)
        pid = self.s.project_id(project) if project else None
        if job_id and not pid:
            r = self.s.q("SELECT project_id FROM jobs WHERE id=?", (job_id,))
            pid = r[0]["project_id"] if r else None
        with self.s.lock:
            cur = self.s.db.execute(
                "INSERT INTO usage(ts,agent_id,project_id,job_id,subtask_id,model,tokens_in,tokens_out,cost,seconds,note)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)", (now(), agent_id, pid, job_id, subtask_id, model, tokens_in,
                                                    tokens_out, c, seconds, note))
            rowid = cur.lastrowid
        from .graphlinks import link_usage
        link_usage(self.s, rowid)
        out = {"recorded": True, "cost": c}
        cap = daily_cap()
        if cap is not None and self.spent_today() >= cap:
            self.s.x("INSERT OR REPLACE INTO meta VALUES('paused', ?)", (f"daily cost cap ${cap} reached",))
            out["paused"] = f"daily cost cap ${cap} reached — orchestration paused"
        return out

    def spent_today(self) -> float:
        return self.s.q("SELECT COALESCE(SUM(cost),0) c FROM usage WHERE ts>=?", (now() - 86400,))[0]["c"]

    def summary(self, by: str = "agent", since: float | None = None) -> list[dict]:
        col = {"agent": "u.agent_id", "project": "u.project_id", "job": "u.job_id", "model": "u.model",
               "day": "date(u.ts,'unixepoch')"}[by]
        rows = self.s.q(f"SELECT {col} k, SUM(tokens_in) tin, SUM(tokens_out) tout, SUM(cost) cost, SUM(seconds) secs,"
                        f" COUNT(*) n FROM usage u WHERE ts>=? GROUP BY k ORDER BY cost DESC", (since or 0,))
        out = []
        for r in rows:
            label = r["k"]
            if by in ("agent", "project", "job") and r["k"]:
                label = self.s.label(r["k"])[1] if by != "project" else (self.s.project_name(r["k"]) or r["k"])
            out.append({"key": r["k"], "label": label or "-", "tokens_in": r["tin"] or 0, "tokens_out": r["tout"] or 0,
                        "cost": round(r["cost"] or 0, 4), "seconds": round(r["secs"] or 0, 1), "calls": r["n"]})
        return out
