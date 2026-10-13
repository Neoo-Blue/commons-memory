# MCP tools reference

Moved from the README in v0.7.1. v0.7 adds messaging (`msg_send`, `msg_recv`, `msg_ack`, `msg_history`, `presence`, `wake_channels`), routing, Drive and breakdown tools; v0.8 adds capture, three-step search, temporal facts, spaces and diagrams (CLI: `commons fact`, `commons sweep`, `commons space`, `commons audit`, `commons diagram`); `commons serve` lists them all via MCP `tools/list`.

## What a client sees (v0.9)
Clients list the **core profile** (21 tools, about 3k tokens): `pickup`, `msg_recv`, `msg_send`, `summon`, `resume`,
`context_load`, `search`, `expand`, `why`, `remember`, `record_decision`, `observe_preference`, `claim_subtask`,
`heartbeat`, `complete_subtask`, `handoff`, `add_leftover`, `report_usage`, `whoami`, plus the two meta-tools:

- `tools(query)`: search the full catalog below; returns name, arguments and a one-line summary.
- `call(tool, args)`: run any tool by name, including unlisted ones.

Change what a client lists with `?tools=core|all|core,name,...` on the MCP URL, per agent with
`commons agent tools NAME PROFILE`, or globally with `COMMONS_TOOLS`. Hidden tools stay callable directly.

v0.9 also adds `summon(agents, reason, wait)` (wake agents now and report who is ready) and `import_items(items,
check)` (bulk, idempotent import; driven by `commons import <source> [--curate] --url ...`).

## Tools

| Area | Tools |
|---|---|
| Memory | `remember`, `search`, `expand`, `timeline`, `conflicts` |
| Graph | `graph_query`, `neighbors`, `export_graph` (mermaid / graphml / json / html) |
| Code | `index_code`, `code_symbols`, `code_impact` (Python, JS/TS, Go, Rust, Java, Ruby, C, C++, C#) |
| Sessions | `session_start`, `checkpoint`, `handoff`, `resume` |
| Reasoning | `record_decision`, `why` |
| Agents | `register_agent`, `list_agents`, `ask_agent`, `delegate`, `inbox`, `respond_task`, `get_task`, `a2a_send` |
| Secrets | `secret_register`, `secret_list`, `secret_request`, `secret_revoke`, `secret_audit` |
| Profiles | `set_profile`, `get_profile`, `rate_agent` |
| Orchestration | `submit_task`, `job_status`, `claim_subtask`, `heartbeat`, `complete_subtask`, `cancel_job`, `kill_switch` (any agent can pause; resuming needs an admin) |
| Continuity | `pickup`, `add_leftover`, `resolve_leftover`, `list_leftovers`, `schedule_recurring`, `run_recurring`, `list_recurring` |
| Preferences | `observe_preference`, `extract_preferences`, `get_preferences`, `confirm_preference`, `forget_preference` |
| Usage | `report_usage`, `usage_summary` |
| Meta and import (v0.9) | `tools`, `call`, `summon`, `import_items`, `whoami` |
| Capture (v0.8) | `observe`, `capture_settings`, `memory_stream` (`<private>…</private>` text is stripped before storage) |
| Connection (v0.8.1) | `whoami` (returns the agent for this key, server version and the marker `commons-ok`; used by the Connect page verify line) |
| 3-step search (v0.8) | `search_index` → `timeline_around` → `get_items` (compact index first, full content only on request) |
| Temporal facts (v0.8) | `fact_set`, `fact_invalidate`, `fact_get` (`as_of=`), `fact_history` (valid_from / valid_to) |
| Spaces (v0.8) | `spaces`, `space_search`, `memory_audit` |
| Diagrams (v0.8) | `diagram` (kind=`code` or `job`; interactive HTML written to Drive) |
| Knowledge base | `kb_get`, `kb_search`, `kb_list`, `kb_refresh` |
| Graph views | `graph_view`, `graph_rebuild` |
| Skill pool | `skill_list`, `skill_search`, `skill_get`, `skill_publish`, `skill_update`, `skill_review`, `skill_history`, `skill_rollback`, `skill_rate`, `skill_fork`, `skill_catalog`, `skill_catalog_add` |
| Research | `research` |
| Models | `provider_add`, `models_refresh`, `models_list`, `agent_models`, `set_role_model`, `routing_table`, `models_probe`, `routing_proposals` (`provider_add` and `set_role_model` need an admin: a provider receives its key) |
| Org chart | `tree`, `tree_find` |
| Librarian | `find`, `librarian_run`, `librarian_status`, `librarian_proposals`, `librarian_approve`, `librarian_undo` |
| MCP hub | `mcp_servers`, `mcp_search`, `mcp_describe`, `mcp_call` |
| Misc | `stats` |

**Token-saving search output** (default `format="text"`):
```
mem_01a12d109ea4… [memory] 2026-10-11 commons-memory/claude-desktop: Search uses reciprocal rank fusion of FTS5 bm25 and vector cosine
dec_01a12d109eb3… [decision] 2026-10-11 commons-memory/claude-desktop: SQLite as default store — because zero-ops, single file
… 14 more (raise max_tokens/limit or refine filters)
```
Filters: `project`, `agent_filter`, `tags`, `kinds`, `entity` (name or id), `since`/`until` (ISO or `7d`, `12h`), `mode` = `hybrid|keyword|semantic`.

**Handoff packet** (what `resume` returns to the next AI):
```json
{"goal": "...", "summary": "...", "state": "...", "decisions": [], "next_steps": [], "open_questions": [],
 "relevant_ids": ["dec_…", "ent_…"], "from_agent": "claude-code", "to_agent": "cursor", "project": "myapp"}
```
