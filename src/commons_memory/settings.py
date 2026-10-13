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
    "plan_chain": (["role:planner", "anthropic/claude-haiku-5-5", "agent", "rules"],
                   "Who splits big jobs into parts, tried in order: role:planner (the model set for that role), any "
                   "provider/model you have a key for, agent (the best live agent plans, on its own subscription), "
                   "rules (keyword splitting, always works). Every planner sees who is live, what each agent is good "
                   "at and how much usage it has left."),
    "capture_verbatim": (True, "Keep every message and reply word for word"),
    "role_presets": (True, "Use a small model from your first provider key (Claude Haiku with an Anthropic key) for "
                           "planning, knowledge-base summaries and preference extraction, unless you set those roles"),
    "capture_diffs": (True, "Keep each agent's code changes (git diff, secrets redacted) with its work, in the job "
                            "workspace"),
    "dup_warnings": (True, "Warn agents when someone else is doing, or already did, the same work"),
    "quota_routing": (True, "Route work only to agents with usage headroom, keeping your reserve"),
    "reserve_pct": (20, "Share of each usage window kept for you, %"),
    "schedule_takeover": (True, "Another agent covers a scheduled run its owner missed"),
    "github_watch": ([], "GitHub repos (owner/name) to watch: a failed CI run, a new issue or a new PR becomes a job for "
                         "the best live agent (uses the vault's GitHub key; works behind NAT)"),
    "phone_ntfy": ("", "Phone approvals: your ntfy topic URL (e.g. https://ntfy.sh/<long-random-topic>); every Needs-you "
                       "question arrives with Yes / No buttons"),
    "graphiti_url": ("", "A Graphiti server (e.g. http://graphiti:8000) to mirror memories into every few minutes, "
                         "as a temporal knowledge graph; `commons graphiti pull` brings its facts back"),
    "dashboard_url": ("", "This dashboard's address as your phone reaches it (LAN or tailnet), for the Yes / No buttons"),
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
    elif isinstance(d, str):
        value = (value or "").strip()
        if value and not value.startswith(("http://", "https://")):
            raise CommonsError(f"{key} must be an http(s) URL")
    elif isinstance(d, list):
        value = [v.strip() for v in (value.split(",") if isinstance(value, str) else value) if str(v).strip()]
        if key == "github_watch":
            import re
            bad = [v for v in value if not re.fullmatch(r"[\w.-]+/[\w.-]+", v)]
            if bad:
                raise CommonsError(f"repos are owner/name; not {bad}")
            bad = []
        elif key == "plan_chain":
            bad = [v for v in value if not (v in ("agent", "rules") or v.startswith("role:") or "/" in v)]
            if bad:
                raise CommonsError(f"plan entries are role:NAME, provider/model, agent or rules; not {bad}")
        else:
            bad = [v for v in value if not (v in ("typesafe", "ask") or v.startswith(("role:", "agent:")) or "/" in v)]
        if bad:
            raise CommonsError(f"judge entries are typesafe, ask, role:NAME, agent:NAME, agent:any or provider/model; not {bad}")
    s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"setting:{key}", json.dumps(value)))
    s.audit(None, "setting.set", key, {"value": value})
    return {key: value}


def listing(s: Store) -> list[dict]:
    return [{"key": k, "value": get(s, k), "default": d, "help": h} for k, (d, h) in DEFAULTS.items()]
