# Commons — one shared brain for all your AIs

**Commons is a memory + coordination hub that every AI agent installs as a skill and connects to over MCP** —
Claude Code, Claude Desktop, Cursor, Codex, ChatGPT, Grok, Muse and custom agents. They share memory, pick up each
other's unfinished work, learn how *you* like things done, and split big tasks between them with debate, review
and verification gates. A local dashboard shows what every AI is doing and what it costs.

```mermaid
flowchart TB
  subgraph Platforms["Agents (each installs the Commons skill)"]
    CC[Claude Code<br/>skill + /commands] & CU[Cursor<br/>.cursor/rules] & CX[Codex<br/>AGENTS.md] & GR[Grok / Muse / ChatGPT<br/>system prompt + HTTP MCP]
  end
  Platforms -- MCP stdio / HTTP + api key --> HUB
  W1[commons worker<br/>claude -p] & W2[commons worker<br/>codex exec] & W3[commons worker<br/>grok cli] -- claim / heartbeat / complete --> HUB
  subgraph HUB[Commons hub]
    MEM[Memory · search · graph · code index]
    ORC[Orchestrator<br/>planner → DAG → allocator<br/>leases · gates · budgets]
    CONT[Sessions · handoffs<br/>leftovers · recurring]
    PREF[User preference model]
    SEC[Secret refs · leases · audit]
    USE[Usage & cost]
  end
  HUB --> DASH[Local dashboard :8766]
```

**v0.2 adds:** installable skills for every platform · capability profiles that learn from outcomes · distributed
orchestration (DAGs, debate, cross-review, consensus, plan→execute→verify, quality gates, budgets, retries) · leftovers and
recurring tasks that carry unfinished work forward · user preference learning injected into every task · headless
`commons worker` for CLI agents · a local web dashboard · usage/cost tracking · kill switch, backups and hardening.
See [FLAWS_AND_GAPS.md](FLAWS_AND_GAPS.md) for known risks.

v0.1 features remain: shared memory with hybrid search and token-budgeted results, one graph across memory/code/sessions,
code indexing, handoffs, reasoning log, agent inbox, secret references, conflict detection.

## v0.3: everything is a graph, and every project has a living knowledge base

**Graph everything.** Schedules and their runs (`run_of`), decisions (`decided_by`, `about` code/entities, `in_project`,
`in_session`), preferences (`applies_to` project / task type / global, `learned_from` agent), workflow patterns
(`uses_pattern`), debate rounds (`argues_in`, `judges`), review verdicts (`reviews`, `approves`/`rejects`), subtask DAG edges
(`depends_on`, `has_type`, `executed_by`), leftovers (`for_schedule`, `carries`), usage (`billed_to` job/project, `spent_by`
agent, `used_model`), leases and secret references (`lease_of`, `held_by`; references only, never values) are linked on write.
`commons graph rebuild` (or the `graph_rebuild` tool) backfills older databases and is idempotent.
Scoped views: `graph_view(scope=project|job|workflow|schedule|node, id)`, `commons graph view --scope job --id job_… --format html`,
and the dashboard Graph page has project, job, workflow and schedule filters plus depth.

**Automatic knowledge base.** Each project, plus `misc` for unassigned memories (auto-clustered into `misc/<topic>`), gets a
wiki with these sections: overview · key decisions & rationale · architecture & components (from the code graph) · glossary &
entities · references (URLs, repos, files) · how-tos & runbooks · people & agents (with spend) · open questions & leftovers ·
timeline · preferences that apply. **Every statement cites its source record ids** (`[dec_…]`, `[mem_…]`, `[ent_…]`).

- Writes mark a bucket dirty. It is rebuilt on read, by `commons scheduler`, or by `commons kb build`, at most every
  `COMMONS_KB_DEBOUNCE` seconds (default 10).
- Extractive summaries by default. `COMMONS_KB_SUMMARIZER="<cmd>"` pipes each section through your LLM; citations are kept.
- MCP tools: `kb_get(project, section, max_tokens)`, `kb_search`, `kb_list`, `kb_refresh`. Every `pickup` and subtask packet
  includes a compact KB digest (~150 tokens).
- `commons kb export ./vault` writes an Obsidian-friendly vault: `<project>/index.md`, one page per section with
  `[[records/<id>]]` wikilinks, and `records/<id>.md` with "Cited in" backlinks.
- Dashboard **Knowledge** page: buckets, section tabs, and clickable citations that open the record, its graph neighbourhood and
  every other KB page that cites it.

| Knowledge base | Graph scoped to a workflow |
|---|---|
| ![kb](docs/img/dashboard-kb.png) | ![graph](docs/img/dashboard-graph-workflow.png) |

## Multi-agent quickstart: one big task across Claude Code + Grok + Cursor

```bash
uv tool install git+https://github.com/Neoo-Blue/commons-memory      # or: pip install git+...
commons init

# 1) connect each agent: registers identity + api key (~/.commons/agents/<name>.key) + capability profile,
#    and installs the skill/rules/MCP config for its platform
commons connect claude-code --platform claude-code --expertise code:0.9,plan:0.8,summarize:0.7 --cost 4
commons connect grok        --platform grok        --expertise debate:0.9,red_team:0.9,research:0.8 --cost 3
commons connect cursor      --platform cursor      --expertise review:0.9,test:0.8,code:0.7 --cost 2

# 2) submit one big task: bullet items become parallel parts, each gated by a review from a different agent
commons submit "Build the checkout service:
- cart API endpoints
- research payment providers
- order confirmation email" --project shop --max-cost 5

# 3) give each platform a headless seat (agents usually aren't running all the time)
commons worker --agent claude-code --preset claude --model claude-sonnet &
commons worker --agent cursor --preset cursor --model gpt-4o &
commons worker --agent grok --cmd "grok-cli --stdin" --model grok-4 &      # any CLI that reads a prompt on stdin

# 4) watch it run
commons dashboard            # http://127.0.0.1:8766
commons status               # DAGs as text
```

Interactive agents use the same tools: in Claude Code, `/commons-checkin` calls `pickup` (ready subtasks, leftovers,
your preferences); `/commons-delegate <task>` submits work; `/commons-handoff` hands off.

Try it without any real AI: `python examples/demo_multi_agent.py` runs three simulated agents (real `commons worker`
processes with a mock CLI) through a decomposed task, a debate, review rejections and rework, a leftover carried into
the next recurring run, and a learned preference applied ([sample output](docs/demo-output.txt)).

## Install the skill on each platform

| Platform | Command | What it writes |
|---|---|---|
| Claude Code | `commons install-skill claude-code [--scope user]` | `.claude/skills/commons/SKILL.md`, `/commons-*` slash commands, plugin manifest, `.mcp.json` |
| Claude Desktop | `commons install-skill claude-desktop` | MCP entry in `claude_desktop_config.json` (+ SKILL.md to upload) |
| Cursor | `commons install-skill cursor` | `.cursor/rules/commons.mdc` (always-on rule) + `.cursor/mcp.json` |
| Codex / others reading AGENTS.md | `commons install-skill codex` | `AGENTS.md` section + `~/.codex/config.toml` MCP server |
| ChatGPT / Grok / Muse / generic | `commons install-skill grok` | `commons-grok-prompt.md` system prompt; connect via HTTP MCP |
| Everything | `commons install-skill all` | all of the above |

The agent-agnostic skill ([`SKILL.md`](src/commons_memory/skills/SKILL.md), Anthropic Agent Skills format) teaches the
loop: **check in** (`pickup`, `resume`) → **pull context** (`search`/`expand`/`why`) → **claim** (`claim_subtask`) →
**record** (`record_decision`, `remember`, `observe_preference`) → **hand off** (`handoff`, `add_leftover`) → **report** (`report_usage`).

## Orchestration

| Pattern | DAG | Quality gate |
|---|---|---|
| `plan_execute_verify` | plan → execute → verify | execute is done only after verify approves |
| `cross_review` | code → review + red_team | code is done only after **both** approve (author can never review own work) |
| `debate` | N debaters (distinct agents) → judge | judge's synthesis is stored as a decision |
| `consensus` | N voters → automatic tally | majority of `VERDICT: approve` |
| `parallel` | plan → parts (each gated by review) → merge/summary | each part |
| explicit / LLM | `dag=[{key,type,deps,gates}]` or `pattern="llm"` with `COMMONS_PLANNER_CMD` | as declared |

- **Allocation**: expertise × learned success rate (Beta-smoothed per agent and task type) − load − cost + speed;
  availability and `max_concurrent` respected; siblings in debates and votes spread across agents.
- **Execution**: atomic claims (no duplicate work), leases + `heartbeat`, expired leases → retry on another agent,
  `max_attempts`, rejected gates → rework with the reviewer's feedback appended, the author's score drops.
- **Budgets**: `budget={"tokens","cost","seconds"}` per job (claims stop → `budget_exceeded`), `COMMONS_DAILY_COST_CAP`
  auto-pauses everything, `kill_switch` / `commons pause` / dashboard button, `cancel_job`.
- **Merging**: final results are merged into the job and saved to memory; judge outputs become decisions.

## Continuity, leftovers and recurring work

- `handoff` next_steps/open_questions and `LEFTOVER:` lines from workers become **leftovers** (de-duplicated).
- `schedule_recurring(name, cron, task)` / `commons schedule`; `commons scheduler` (or system cron + `--once`) starts due runs;
  **open leftovers from earlier runs become subtasks of the next run**.
- `pickup` gives each agent ready subtasks, its inbox and leftovers ranked by priority × its expertise, plus your preferences.

## Your preferences, learned once

`observe_preference`, or automatically from corrections (`extract_preferences("No, use tabs instead…")`, rejected
review feedback, workers' `PREFERENCE:` lines). Each preference has confidence, source, evidence, scope
(global / project / task type) and decays over time (half-life `COMMONS_PREF_HALF_LIFE_DAYS`, default 120) unless confirmed or
seen again. Relevant preferences are **injected into every `pickup` and subtask packet**, within a token budget.
Privacy: `commons prefs list|add|confirm|forget|forget-all|export` or the dashboard's Preferences page. Plug in an
LLM extractor with `COMMONS_PREF_EXTRACTOR="<cmd>"`.

## Workers (headless execution)

`commons worker --agent X (--preset claude|codex|cursor|gemini | --cmd "<shell template>")` loops: claim → render a
prompt (brief, upstream results fenced as untrusted, preferences, protocol) → run the CLI with the prompt on stdin
(also `{prompt_file}`, `$COMMONS_SUBTASK_ID`, `$COMMONS_TASK_TYPE`) → heartbeat while running → parse
`VERDICT:` / `LEFTOVER:` / `PREFERENCE:` / `TOKENS:` → complete (a non-zero exit means the attempt failed and is retried).
Use `--url http://hub:8765/mcp --api-key …` to work against a remote hub. See [docs/workers.md](docs/workers.md).

## Dashboard

`commons dashboard` (or `commons serve --dashboard`) → http://127.0.0.1:8766. It binds to localhost only; set `--token`
or `COMMONS_DASHBOARD_TOKEN` to bind elsewhere. Pages: live agent activity (claims, lease countdowns, last seen,
learned scores), sessions and leftovers, orchestration DAGs with debate transcripts and gate verdicts, cost and usage charts by
agent, project, job, model and day, a memory browser with conflict resolution, editable preferences, an interactive graph,
secret references, leases and audit log (never values), recurring schedules, and a kill switch. Prometheus metrics are at `/metrics`.

![dashboard](docs/img/dashboard-overview.png)

Costs come from `report_usage` (agents) and workers, priced with a configurable table (`COMMONS_PRICES` or
`~/.commons/prices.json`, `{"model-prefix": [usd_per_M_in, usd_per_M_out]}`).

## Basics (v0.1)

## Quickstart

```bash
# install (pick one)
uv tool install git+https://github.com/Neoo-Blue/commons-memory
pip install git+https://github.com/Neoo-Blue/commons-memory

commons init                      # creates ~/.commons/commons.db, prints client config snippets
commons index . --project myapp   # index a repo into the graph
commons remember "Payments use Stripe webhooks; retries are idempotent" --project myapp
commons search "stripe retries"
commons export-graph --format html -o graph.html   # open in a browser
commons serve                     # MCP over stdio
commons serve --transport http --port 8765         # MCP streamable HTTP at http://127.0.0.1:8765/mcp
```

Optional better embeddings: `pip install "commons-memory[embed] @ git+https://github.com/Neoo-Blue/commons-memory"` and set
`COMMONS_EMBEDDER=fastembed` (BAAI/bge-small, local ONNX). Default is a dependency-free hashing embedder;
`COMMONS_EMBEDDER=none` gives keyword-only.

## Connect your AIs

All clients should point at the **same `COMMONS_DB`** (stdio) or the same HTTP server, so they share memory.

**Claude Desktop** — `claude_desktop_config.json`
```json
{
  "mcpServers": {
    "commons": {
      "command": "commons",
      "args": ["serve"],
      "env": { "COMMONS_AGENT": "claude-desktop", "COMMONS_DB": "/Users/you/.commons/commons.db" }
    }
  }
}
```

**Claude Code**
```bash
claude mcp add commons -e COMMONS_AGENT=claude-code -- commons serve
```

**Cursor** — `.cursor/mcp.json` (or `~/.cursor/mcp.json`)
```json
{
  "mcpServers": {
    "commons": { "command": "commons", "args": ["serve"], "env": { "COMMONS_AGENT": "cursor" } }
  }
}
```

**Generic / remote (streamable HTTP)** — ChatGPT connectors, custom agents, other MCP clients
```bash
commons register-agent my-agent --vendor acme     # prints an api_key (shown once)
commons serve --transport http --host 0.0.0.0 --port 8765
```
```json
{ "mcpServers": { "commons": { "url": "http://your-host:8765/mcp" } } }
```
Over HTTP, writes require the agent's `api_key` argument (set `COMMONS_REQUIRE_KEYS=0` or `--no-auth` for local dev).
For stdio you can set `COMMONS_API_KEY` in the client env instead of passing it per call. Put the HTTP server
behind TLS (reverse proxy) before exposing it beyond localhost.

## Tools

| Area | Tools |
|---|---|
| Memory | `remember`, `search`, `expand`, `timeline`, `conflicts` |
| Graph | `graph_query`, `neighbors`, `export_graph` (mermaid / graphml / json / html) |
| Code | `index_code`, `code_symbols`, `code_impact` |
| Sessions | `session_start`, `checkpoint`, `handoff`, `resume` |
| Reasoning | `record_decision`, `why` |
| Agents | `register_agent`, `list_agents`, `ask_agent`, `delegate`, `inbox`, `respond_task`, `get_task`, `a2a_send` |
| Secrets | `secret_register`, `secret_list`, `secret_request`, `secret_revoke`, `secret_audit` |
| Profiles | `set_profile`, `get_profile`, `rate_agent` |
| Orchestration | `submit_task`, `job_status`, `claim_subtask`, `heartbeat`, `complete_subtask`, `cancel_job`, `kill_switch` |
| Continuity | `pickup`, `add_leftover`, `resolve_leftover`, `list_leftovers`, `schedule_recurring`, `run_recurring`, `list_recurring` |
| Preferences | `observe_preference`, `extract_preferences`, `get_preferences`, `confirm_preference`, `forget_preference` |
| Usage | `report_usage`, `usage_summary` |
| Knowledge base | `kb_get`, `kb_search`, `kb_list`, `kb_refresh` |
| Graph views | `graph_view`, `graph_rebuild` |
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

## Architecture

```mermaid
flowchart LR
  subgraph Clients
    CD[Claude Desktop/Code] ---|stdio| GW
    CU[Cursor] ---|stdio| GW
    GPT[ChatGPT / remote agents] ---|streamable HTTP + api_key| GW
  end
  GW[MCP gateway<br/>FastMCP server] --> RED[Redaction scanner<br/>on every write]
  RED --> ST[(SQLite<br/>items · FTS5 · embeddings<br/>entities · relations<br/>sessions · inbox · audit)]
  GW --> CI[Code indexer<br/>Python ast · JS/TS] --> ST
  GW --> BR[Secret broker<br/>leases · TTL · audit] --> BE{{env:// · vault:// OpenBao · op:// · infisical://}}
  GW --> A2A[A2A client] --> EXT[External A2A agents]
  ST --> EXP[Mermaid · GraphML · HTML viewer]
  ST -. v2 .-> PG[(Postgres + pgvector)]
```

- **Stable ids** (`mem_`, `dec_`, `fct_`, `hof_`, `chk_`, `ent_`, `prj_`, `ses_`, `agt_`, `tsk_`, `lea_`) are time-sortable and
  global; a `relations` table links any id to any id (`in_project`, `authored_by`, `in_session`, `mentions`,
  `related_to`, `defines`, `imports`, `calls`, `contradicts`, `assigned_to`, …), so "decision X ↔ function Y ↔ session S ↔ agent A" is one query.
- **Search** fuses FTS5 BM25 and vector cosine with reciprocal-rank fusion, then packs results until `max_tokens`.
- **Storage interface** (`backends.py`) lets a Postgres/pgvector adapter slot in (DDL included; implementation is v2).

## Security model

1. **No raw secrets in memory.** Every write (memories, titles, metadata, session state, handoffs, inbox tasks,
   A2A messages) passes a gitleaks-style scanner (private keys, AWS/GitHub/Slack/OpenAI/Stripe/Google keys, JWTs, bearer
   tokens, `password=`-style assignments, credentials in URLs) and is stored as `[REDACTED:<kind>]`.
2. **References, not values.** Secrets are registered as `env://NAME`, `vault://mount/path#key` (Vault/OpenBao KV v2),
   `op://vault/item/field` (1Password CLI) or `infisical://…` (adapter interface). Refs can be limited to named agents and a max TTL.
3. **Short-lived, agent-bound leases.** `secret_request` returns a lease handle (default 5 min, max 1 h) — never the value.
   The value is resolved only by `commons secret-exec <lease> --as VAR -- <cmd>`, which injects it into a child
   process environment, so it never enters a model's context. Leases are bound to the requesting agent, expire, and can be revoked
   individually, per agent, or per ref.
4. **Audit.** Register / lease / resolve / deny / revoke / redaction events go to an append-only audit table (no values).
5. **Identity & provenance.** Agents get API keys (only SHA-256 hashes are stored). Every record carries `agent_id` and an
   `authenticated` flag. HTTP mode requires keys for writes by default.
6. **Poisoning & contradictions.** Facts with `subject/predicate/value` are checked against existing facts; contradictions are flagged
   (`⚠conflict` in search), linked with `contradicts` edges, and resolved via `conflicts(resolve_id, keep_id)` which supersedes the loser.

Threats not yet covered in v1: per-project ACLs/row-level security, OAuth 2.1 for HTTP, rate limiting, encryption at rest
(use full-disk encryption or SQLCipher), egress allow-lists for A2A.

## Comparison & credits

Commons composes ideas from excellent projects — use them directly if they fit you better:

| Project | What it does great | How Commons relates |
|---|---|---|
| [Graphiti / Zep](https://github.com/getzep/graphiti) | Bi-temporal knowledge graph, hybrid retrieval | Inspired conflict/supersede model; Graphiti adapter is on the roadmap |
| [mem0 / OpenMemory](https://github.com/mem0ai/mem0) | Fact extraction, cross-client MCP memory | Similar "one memory for all clients" goal; Commons adds code graph, handoffs, reasoning, secrets |
| [Cognee](https://github.com/topoteretes/cognee) | Document + code knowledge graphs (ECL pipelines) | Commons is a lighter single-file store; Cognee is richer for doc ingestion |
| [Serena](https://github.com/oraios/serena) | LSP-powered semantic code tools for agents | Commons' code index is static & cheap; Serena-style live LSP lookups planned |
| [A2A protocol](https://github.com/a2aproject/A2A) | Agent-to-agent messaging standard | Commons ships an A2A client stub; full A2A server endpoint planned |
| [basic-memory](https://github.com/basicmachines-co/basic-memory), [MCP memory server](https://github.com/modelcontextprotocol/servers) | Human-readable / minimal MCP memory | Markdown export planned for human audit |

## Roadmap

- **v0.1**: memory, search, graph, code index, sessions/handoff, reasoning log, inbox, secret refs, CLI.
- **v0.3 (this release)**: everything is linked into the graph (+ `graph rebuild` backfill and scoped views); automatic,
  cited, per-project knowledge base with misc topic clustering, Obsidian export, pickup digests and a dashboard KB browser.
- **v0.2**: skills/installers, capability profiles and learning, orchestration patterns and gates, leftovers and
  recurring runs, preference learning, workers, dashboard, usage and cost tracking, kill switch, backup and export.
- **next**: LLM-written KB prose by default when a summarizer is configured, embedding-based topic clustering; Postgres + pgvector backend; Graphiti adapter; tree-sitter for more languages; LLM planner, summarizer and preference
  extractor presets; full A2A server endpoint; OAuth 2.1 and per-project ACLs; trust scores weighting memory by provenance;
  OpenTelemetry traces; dashboard live updates over SSE; push notifications to wake agents instead of polling.

## Development

```bash
uv venv && uv pip install -e ".[dev]" && uv run pytest -q
```

MIT licensed.
