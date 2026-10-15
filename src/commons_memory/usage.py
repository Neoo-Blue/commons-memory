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
    # current generation (platform.claude.com pricing, 2026-09); the bare family names above stay for old ids
    "claude-opus-5-5": (4.0, 20.0), "claude-opus-5": (5.0, 25.0), "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0), "claude-opus-4-6": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0), "claude-haiku-4-5": (1.0, 5.0), "claude-fable-5": (10.0, 50.0),
    "claude-haiku-5": (1.0, 5.0),  # ponytail: Haiku 5.5 price not published to us yet; assumed = Haiku 4.5, override in prices.json
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
               subtask_id: str | None = None, note: str = "", cached_tokens: int = 0) -> dict:
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
            if cached_tokens:
                self.s.db.execute("UPDATE usage SET cached_tokens=? WHERE id=?", (cached_tokens, rowid))
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
               "day": ("to_char(to_timestamp(u.ts) AT TIME ZONE 'UTC', 'YYYY-MM-DD')" if self.s.dialect == "postgres"
                       else "date(u.ts,'unixepoch')")}[by]
        src = ("(SELECT ts, agent_id, project_id, job_id, model, tokens_in, tokens_out, cost, seconds, 1 AS calls FROM usage "
               "UNION ALL SELECT ts, NULLIF(agent_id,''), NULLIF(project_id,''), NULL, NULLIF(model,''), tokens_in, "
               "tokens_out, cost, seconds, calls FROM usage_daily)")  # pruned rows live on as daily rollups
        rows = self.s.q(f"SELECT {col} k, SUM(tokens_in) tin, SUM(tokens_out) tout, SUM(cost) cost, SUM(seconds) secs,"
                        f" SUM(calls) n FROM {src} u WHERE ts>=? GROUP BY k ORDER BY cost DESC", (since or 0,))
        if by == "project":  # one row per project as the Projects page groups it, not per repo or worktree name
            from .projects import _groups, _rows
            prow = _rows(self.s)
            root_of = {m: root for root, ids in _groups(prow).items() for m in ids}
            agg: dict = {}
            for r in rows:
                k = root_of.get(r["k"], r["k"]) if r["k"] else None
                a = agg.setdefault(k, {"tin": 0, "tout": 0, "cost": 0.0, "secs": 0.0, "n": 0})
                for f in ("tin", "tout", "cost", "secs", "n"):
                    a[f] += r[f] or 0
            rows = [{"k": k} | v for k, v in sorted(agg.items(), key=lambda kv: -kv[1]["cost"])]
            label_of = {pid: (r["title"] or r["name"]) for pid, r in prow.items()}
        out = []
        for r in rows:
            label = r["k"]
            if by in ("agent", "job") and r["k"]:
                label = self.s.label(r["k"])[1]
            elif by == "project":
                label = label_of.get(r["k"], r["k"]) if r["k"] else "No project (Commons' own model calls)"
            out.append({"key": r["k"], "label": label or "-", "tokens_in": r["tin"] or 0, "tokens_out": r["tout"] or 0,
                        "cost": round(r["cost"] or 0, 4), "seconds": round(r["secs"] or 0, 1), "calls": r["n"]})
        return out
