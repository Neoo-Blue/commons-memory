"""Tool profiles: which tools a client sees in tools/list (v0.9).

All 150 tool schemas cost ~33k tokens in every connected AI's context, every session. The default `core` profile
lists the daily-routine tools plus two meta-tools, `tools` (search the whole catalog) and `call` (run any tool by
name), so nothing is out of reach. Hidden tools stay callable directly; only the listing shrinks.

Profile resolution, first match wins: `?tools=` on the MCP URL, the agent's stored profile (`commons agent tools NAME
PROFILE`), `COMMONS_TOOLS`, then `core`. A profile is `core`, `all`, or a comma list of tool names that may start
with `core` (`core,submit_task,job_status`).

Header-authenticated clients also don't see the `agent`/`api_key` identity parameters: the transport already says
who they are.
"""
from __future__ import annotations

import contextvars
import json
import os

CORE = ["turn", "pickup", "start_work", "finish_work", "msg_recv", "msg_send", "summon", "resume", "search", "expand", "why",
        "remember", "record_decision", "observe_preference", "submit_task", "claim_subtask", "heartbeat", "complete_subtask", "handoff",
        "add_leftover", "report_usage", "whoami", "board", "board_add", "board_update", "credentials", "use_credential", "skill", "skill_help",
        "infra",
        "tools", "call"]  # one-time or occasional tools (set_profile, vault_deposit, vault_generate, handover_status,
# whats_new, status_update, subtask_pause, context_load, ...) stay one call() away; pickup tells agents when they apply
IDENTITY = ("agent", "api_key")

REQUESTED: contextvars.ContextVar[str | None] = contextvars.ContextVar("commons_tools", default=None)


def _agent_profile(store, aid: str | None) -> str | None:
    if not aid:
        return None
    r = store.q("SELECT profile FROM agents WHERE id=?", (aid,))
    try:
        return (json.loads(r[0]["profile"] or "{}") if r else {}).get("tools") or None
    except (ValueError, AttributeError):
        return None


def set_agent_profile(store, name: str, profile: str) -> dict:
    a = store.get_agent(name)
    if not a:
        from .store import CommonsError
        raise CommonsError(f"no such agent: {name}")
    r = store.q("SELECT profile FROM agents WHERE id=?", (a["id"],))
    try:
        prof = json.loads(r[0]["profile"] or "{}")
    except ValueError:
        prof = {}
    if profile:
        prof["tools"] = profile
    else:
        prof.pop("tools", None)
    store.x("UPDATE agents SET profile=? WHERE id=?", (json.dumps(prof), a["id"]))
    return {"agent": a["name"], "tools": profile or None}


def _compact(schema: dict) -> dict:
    """Smaller JSON Schema for tools/list: no auto-generated titles, `X | None` shown as X, empty defaults dropped.
    Every tool schema is paid for in every client's context on every session."""
    sch = {k: v for k, v in schema.items() if k != "title"}
    alts = sch.get("anyOf")
    if isinstance(alts, list):
        real = [a for a in alts if a.get("type") != "null"]
        if len(real) == 1:
            sch.pop("anyOf")
            sch = {**{k: v for k, v in real[0].items() if k != "title"}, **sch}
    if sch.get("default") in ("", None) and "default" in sch:
        sch.pop("default")
    if isinstance(sch.get("items"), dict):
        sch["items"] = _compact(sch["items"])
    return sch


def principal() -> str | None:
    from .auth import ANON, PRINCIPAL
    p = PRINCIPAL.get()
    return p if p and p != ANON else None


def resolve(store) -> set[str] | None:
    """Visible tool names for the current caller, or None for all."""
    spec = REQUESTED.get() or _agent_profile(store, principal()) or os.environ.get("COMMONS_TOOLS") or "core"
    names = {n.strip() for n in spec.split(",") if n.strip()}
    if "all" in names:
        return None
    if "core" in names:
        names = (names - {"core"}) | set(CORE)
    return names


def install(mcp, store) -> None:
    """Filter tools/list per caller, hide identity params from header-authenticated callers, add `tools`/`call`."""
    from mcp.server.fastmcp.exceptions import ToolError
    tm = mcp._tool_manager

    @mcp.tool()
    def tools(query: str = "", limit: int = 25) -> str:
        """Search the full Commons tool catalog (about 150 tools; your client lists only a few). Returns name,
        summary and arguments; run any of them with call(tool, args). Empty query lists every tool name."""
        allt = all_tools()
        if not query.strip():
            return " ".join(sorted(t.name for t in allt))
        words = [w for w in query.lower().split() if w]
        scored = []
        for t in allt:
            hay = f"{t.name} {t.name.replace('_', ' ')} {t.description or ''}".lower()
            s = sum(3 if w in t.name else 1 for w in words if w in hay)
            if s:
                scored.append((-s, t.name, t))
        out = []
        for _, _, t in sorted(scored)[:limit]:
            props = {k: v for k, v in (t.parameters or {}).get("properties", {}).items() if k not in IDENTITY}
            req = set((t.parameters or {}).get("required", []))
            args = ", ".join(f"{k}{'*' if k in req else ''}" for k in props)
            summary = (t.description or "").strip().split("\n")[0][:200]
            out.append(f"{t.name}({args}): {summary}")
        return "\n".join(out) or f"no tool matches {query!r}; call tools() for the full list"

    @mcp.tool()
    async def call(tool: str, args: dict | None = None) -> str:
        """Run any Commons tool by name, including ones your client doesn't list. Find names and arguments with
        tools(query). Example: call(tool="submit_task", args={"task": "...", "project": "web"})."""
        if tool in ("call",):
            raise ToolError("call cannot call itself")
        if tm.get_tool(tool) is None:
            raise ToolError(f"unknown tool {tool!r}; search with tools(query)")
        res = await tm.call_tool(tool, args or {}, context=mcp.get_context(), convert_result=True)
        if isinstance(res, tuple):  # (content blocks, structured)
            res = res[0]
        if isinstance(res, dict):
            return json.dumps(res, default=str)
        return "\n".join(getattr(c, "text", "") or str(c) for c in res)

    orig_list = tm.list_tools

    def all_tools():
        return orig_list()

    def listed():
        allowed = resolve(store)
        hide_identity = principal() is not None
        out = []
        for t in orig_list():
            if allowed is not None and t.name not in allowed:
                continue
            props = (t.parameters or {}).get("properties", {})
            drop = IDENTITY if hide_identity and all(k in props for k in IDENTITY) else ()
            params = {k: v for k, v in (t.parameters or {}).items() if k != "title"}
            params["properties"] = {k: _compact(v) for k, v in props.items() if k not in drop}
            if "required" in params:
                params["required"] = [r for r in params["required"] if r not in drop]
            desc = (t.description or "").split("\nAliases:")[0].strip()  # aliases still work, they just aren't listed
            t2 = t.model_copy(update={"parameters": params, "description": desc})
            t2.__dict__["output_schema"] = None  # FastMCP's generic {"result": str} wrapper: ~40 tokens a tool, no information
            out.append(t2)
        return out

    tm.all_tools = all_tools  # type: ignore[attr-defined]
    tm.list_tools = listed
