"""Feature switches the user picks on the dashboard's Setup page (stored in the hub; env vars still work as defaults)."""
from __future__ import annotations

import json
import os

from .store import CommonsError, Store

DEFAULTS = {
    "judge_chain": (["typesafe", "anthropic/claude-haiku-5-5", "role:gatekeeper", "agent:any", "ask"],
                    "Who decides on credential use, tried in order: typesafe (Jev), any provider/model you have a key "
                    "for (anthropic/..., openai/..., google/..., xai/..., openrouter/..., ollama/...), role:gatekeeper "
                    "(the model set for that role), agent:any or agent:NAME (a live connected agent judges, on its own "
                    "subscription; never the one asking), ask (you decide). Entries that can't answer are skipped; "
                    "you are always the last stop."),
    "capture_verbatim": (True, "Keep every message and reply word for word"),
    "dup_warnings": (True, "Warn agents when someone else is doing, or already did, the same work"),
    "quota_routing": (True, "Route work only to agents with usage headroom, keeping your reserve"),
    "reserve_pct": (20, "Share of each usage window kept for you, %"),
    "schedule_takeover": (True, "Another agent covers a scheduled run its owner missed"),
}
ENV = {"reserve_pct": "COMMONS_QUOTA_RESERVE"}


def get(s: Store, key: str):
    r = s.q("SELECT value FROM meta WHERE key=?", (f"setting:{key}",))
    if r:
        return json.loads(r[0]["value"])
    if key in ENV and os.environ.get(ENV[key]):
        return type(DEFAULTS[key][0])(os.environ[ENV[key]])
    return DEFAULTS[key][0]


def put(s: Store, key: str, value) -> dict:
    if key not in DEFAULTS:
        raise CommonsError(f"unknown setting {key!r}")
    d = DEFAULTS[key][0]
    if isinstance(d, bool):
        value = value in (True, "true", "on", "1", 1)
    elif isinstance(d, int):
        value = int(value)
        if not 0 <= value <= 90:
            raise CommonsError("reserve_pct is 0-90")
    elif isinstance(d, list):
        value = [v.strip() for v in (value.split(",") if isinstance(value, str) else value) if str(v).strip()]
        bad = [v for v in value if not (v in ("typesafe", "ask") or v.startswith(("role:", "agent:")) or "/" in v)]
        if bad:
            raise CommonsError(f"judge entries are typesafe, ask, role:NAME, agent:NAME, agent:any or provider/model; not {bad}")
    s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"setting:{key}", json.dumps(value)))
    s.audit(None, "setting.set", key, {"value": value})
    return {key: value}


def listing(s: Store) -> list[dict]:
    return [{"key": k, "value": get(s, k), "default": d, "help": h} for k, (d, h) in DEFAULTS.items()]
