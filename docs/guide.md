# Guide: multi-agent work, skills, orchestration, workers, dashboard

Moved from the README in v0.7.1.

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
or `COMMONS_DASHBOARD_TOKEN` to bind elsewhere. With `serve --transport http --host <ip> --dashboard`, the dashboard follows
`--host` (since 0.5.1); override with `--dashboard-host`. Pages: live agent activity (claims, lease countdowns, last seen,
learned scores), sessions and leftovers, orchestration DAGs with debate transcripts and gate verdicts, cost and usage charts by
agent, project, job, model and day, a memory browser with conflict resolution, editable preferences, an interactive graph,
secret references, leases and audit log (never values), recurring schedules, and a kill switch. Prometheus metrics are at `/metrics`.

![dashboard](docs/img/dashboard-overview.png)

Costs come from `report_usage` (agents) and workers, priced with a configurable table (`COMMONS_PRICES` or
`~/.commons/prices.json`, `{"model-prefix": [usd_per_M_in, usd_per_M_out]}`).
