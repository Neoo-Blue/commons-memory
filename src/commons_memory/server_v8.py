"""v0.8 MCP tools: observations/capture, progressive-disclosure search, temporal facts, scoped spaces, diagrams."""
from __future__ import annotations

import json

from .store import CommonsError


def _js(o):
    return json.dumps(o, ensure_ascii=False, default=str, separators=(",", ":"))


def register_v8(mcp, store, who):
    from .capture import KINDS, Capture
    cap = Capture(store)

    def me(agent, api_key):
        aid, _ = who(agent, api_key)
        if not aid:
            raise CommonsError("identify yourself: pass agent (or use an agent key)")
        return aid

    @mcp.tool()
    def observe(kind: str, text: str, project: str = "", why: str = "", agent: str = "", api_key: str = "") -> str:
        """Record a typed observation as you work: kind = decision|bugfix|discovery|security_alert|todo|change.
        decision -> reasoning log, todo -> leftover, security_alert -> notification. <private>…</private> is dropped."""
        if kind not in KINDS:
            raise CommonsError(f"kind must be one of {', '.join(KINDS)}")
        aid = me(agent, api_key)
        from .capture import strip_private
        text = strip_private(text)
        if kind == "decision":
            r = store.record_decision(text, why or "(observed)", agent_id=aid, project=project or None)
        else:
            r = store.add_item("observation", text, title=f"[{kind}] {text[:70]}", project=project or None,
                               agent_id=aid, tags=["observation", kind], meta={"source": "mcp:observe"})
            if kind == "todo":
                from .orchestrator import Orchestrator
                Orchestrator(store).add_leftover(text, project=project or None, source_id=r["id"], agent_id=aid)
        return _js({"id": r["id"], "kind": kind})

    @mcp.tool()
    def capture_settings(project: str, enabled: str = "", tools: str = "", prompts: str = "", inject: str = "",
                         agent: str = "", api_key: str = "") -> str:
        """Per-project auto-capture switches (hooks): enabled/tools/prompts/inject = on|off; empty = unchanged."""
        me(agent, api_key)
        kw = {k: (v == "on") if v else None for k, v in
              (("enabled", enabled), ("tools", tools), ("prompts", prompts), ("inject", inject))}
        return _js(cap.configure(project, **kw))

    @mcp.tool()
    def memory_stream(project: str = "", limit: int = 30) -> str:
        """Latest captured observations (prompts, tool calls, typed observations) across agents."""
        return _js(cap.stream(project or None, limit=limit))
