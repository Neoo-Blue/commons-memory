"""Consistent, forgiving tool arguments for every MCP tool.

Before a tool runs: camelCase/kebab-case keys become snake_case, case is ignored, and common aliases map onto the
real parameter (text->content, decision->what, rationale->why, q->query, file->path, ...) when that parameter was not
given. Unknown arguments, missing required ones and type errors come back as one readable message that lists what the
tool accepts, instead of a raw validation dump. Each tool's description lists the aliases it accepts.
"""
from __future__ import annotations

import re

ALIASES: dict[str, list[str]] = {
    "content": ["text", "body", "note", "memory", "message"],
    "body": ["text", "content", "message"],
    "text": ["content", "body"],
    "query": ["q", "search", "question", "text", "topic"],
    "what": ["decision", "title"],
    "why": ["rationale", "reason", "because"],
    "project": ["project_name", "projectname", "repo", "workspace"],
    "path": ["file", "filename", "file_path", "filepath"],
    "task": ["prompt", "description", "request"],
    "job_id": ["job", "jobid"],
    "subtask_id": ["subtask", "task_id"],
    "to": ["recipient", "target"],
    "tags": ["labels", "tag"],
    "limit": ["n", "max_results", "top_k", "k"],
    "max_tokens": ["token_budget", "budget_tokens"],
    "agent": ["agent_name", "as_agent"],
    "title": ["name", "subject"],
    "ref": ["id", "ref_id"],
    "result": ["output", "answer"],
    "ids": ["id_list", "item_ids", "items"],
    "anchor": ["id", "item_id", "around"],
    "kind": ["type", "category"],
    "as_of": ["asof", "at_time", "when"],
}


IDENTITY_ARGS = {"api_key", "agent", "apikey"}


def _snake(k: str) -> str:
    k = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", k).replace("-", "_")
    return k.lower()


def normalize(name: str, params: dict, required: list[str], args: dict) -> dict:
    from .store import CommonsError
    props = set(params)
    out, unknown = {}, []
    for k, v in (args or {}).items():
        sk = _snake(k)
        if sk in props:
            out[sk] = v
        else:
            unknown.append((k, v))
    for k, v in unknown:
        sk = _snake(k)
        tgt = next((p for p in props if sk in ALIASES.get(p, []) and p not in out), None)
        if tgt:
            out[tgt] = v
        elif sk in IDENTITY_ARGS:
            continue  # clients may pass api_key/agent on every call; read-only tools take identity from the transport
        else:
            raise CommonsError(f"{name}: unknown argument '{k}'. Accepted: {_sig(params, required)}")
    missing = [r for r in required if r not in out or out[r] in (None, "")]
    if missing:
        raise CommonsError(f"{name} needs {', '.join(repr(m) for m in missing)}. Accepted: {_sig(params, required)}")
    return out


def _sig(params: dict, required: list[str]) -> str:
    parts = []
    for p, sch in params.items():
        if p in ("api_key",):
            continue
        t = sch.get("type") or "/".join(x.get("type", "?") for x in sch.get("anyOf", []) if x.get("type") != "null") or "any"
        al = [a for a in ALIASES.get(p, []) if a not in params]
        parts.append(f"{p}{'*' if p in required else ''}:{t}" + (f" (alias {'/'.join(al)})" if al else ""))
    return ", ".join(parts) + "  (* = required)"


def _friendly(name: str, e: Exception, params, required) -> str:
    msg = str(e)
    m = re.findall(r"\n(\w+)\n\s+(.+?) \[type=", msg)
    if m:
        msg = "; ".join(f"'{f}': {why}" for f, why in m)
    return f"{name}: invalid arguments ({msg}). Accepted: {_sig(params, required)}"


def install(mcp) -> None:
    from mcp.server.fastmcp.exceptions import ToolError
    tm = mcp._tool_manager
    for t in tm.list_tools():
        props = (t.parameters or {}).get("properties", {})
        al = [f"{a}->{p}" for p in props for a in ALIASES.get(p, []) if a not in props]
        if al and "Aliases:" not in (t.description or ""):
            t.description = (t.description or "").rstrip() + f"\nAliases: {', '.join(al)}."
    orig = tm.call_tool

    async def call(name, arguments, *a, **kw):
        tool = tm.get_tool(name)
        if tool is None:
            raise ToolError(f"Unknown tool: {name}. Call tools/list for the catalog.")
        props = (tool.parameters or {}).get("properties", {})
        req = list((tool.parameters or {}).get("required", []))
        try:
            arguments = normalize(name, props, req, arguments or {})
        except Exception as e:  # noqa: BLE001
            raise ToolError(str(e)) from e
        try:
            return await orig(name, arguments, *a, **kw)
        except ToolError as e:
            if "validation error" in str(e):
                raise ToolError(_friendly(name, e, props, req)) from e
            raise
    tm.call_tool = call
