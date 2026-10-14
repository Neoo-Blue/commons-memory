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
| `auto` (default) | the plan chain splits it (below) | as the planner declares |
| explicit | `dag=[{key,type,deps,gates,agent}]` (agent pins a node, retries included) | as declared |

- **Planning** (`plan_chain`, Setup page): the model set for the planner role → any provider/model with a key
  (default `anthropic/claude-haiku-5-5`) → `agent`: the best live agent at planning writes the plan as the job's first
  step, and its JSON answer becomes the parts → `rules`: the keyword patterns above. Planners see who is live, what
  each agent is good at and its usage left. A big `msg_send(type=task)` is planned the same way, favouring the
  recipient. An agent holding a subtask too big for one sitting calls `split_subtask(subtask_id, parts)`: the parts
  are routed like any step and the subtask comes back to it to merge their results.
- **Allocation**: expertise × learned success rate (Beta-smoothed per agent and task type) − load − cost + speed
  (declared, and learned from how fast each agent finishes each kind of work), + usage left (after your reserve and
  the cost of one task; room that would reset unused soon counts a little more), then model quality, price and
  liveness; availability and `max_concurrent` respected; siblings in debates and votes spread across agents.
- **Execution**: atomic claims (no duplicate work), leases + `heartbeat`, expired leases → retry on another agent,
  `max_attempts`, rejected gates → rework with the reviewer's feedback appended, the author's score drops.
- **Budgets**: `budget={"tokens","cost","seconds"}` per job (claims stop → `budget_exceeded`), `COMMONS_DAILY_COST_CAP`
  auto-pauses everything, `kill_switch` / `commons pause` / dashboard button, `cancel_job`.
- **Merging**: final results are merged into the job and saved to memory; judge outputs become decisions.

### When splitting doesn't happen as expected

| What you see | Likely cause | What to do |
|---|---|---|
| A job has one subtask | The task looked small (no list of parts, under 150 words), or the planner judged it one step | Give it as a bulleted list, or pass an explicit `dag` |
| A hand-off wasn't split | `reroute=false`, skill help, or the task looked small: those stay whole by design | Use `submit_task` for work you want split |
| `pattern` is `auto` and the parts look generic | No planner could answer (no model key, no live agent), so the keyword rules split it | Add a provider key on Setup, or keep a daemon-backed agent running; the dashboard audit log shows `plan.error` / `plan.unparsed` |
| One `plan` step, nothing else yet | A live agent is writing the plan; its answer becomes the parts | Wait, or check that agent's daemon. If it fails or times out (one try), the keyword rules take over |
| A part stays pending | Its dependencies aren't done, its agent is at its usage limit (`not_before`), or nobody with a profile can take it | `job_breakdown` shows each part's agent and routing reason; `commons scheduler` escalates stuck work |
| A split subtask never merges | It waits for every part, then goes back to the agent that split it | Check that the parts finished and that the splitting agent is still running; the merge step escalates if it sits |
| The job stays `running` | Some step isn't done: an open review, a failed part, a cancelled or over-budget job | Open the job in Task breakdown; a failed part fails the job |

## Six-stage workflow: /spec, /plan, /build, /test, /review, /ship

`submit_task(task, pattern="sdlc")` (or the `/sdlc` slash command) runs one job through a fixed spine:

| Stage | Who | Done when |
|---|---|---|
| define (`/spec`) | a lead writes `define/spec.md` with numbered criteria (`AC1: ...`) | you approve it (board question or phone) |
| plan (`/plan`) | a lead writes the PRD, contracts and the build workstreams | a reviewer from another model family approves |
| build (`/build`) | a lead hands workstreams out as task cards and merges the diffs | verify, two reviews and you all approve |
| verify (`/test`) | runs and writes tests, one `AC1: PASS/FAIL` line per criterion | every criterion passes |
| review (`/review`) | two reviewers, cross-family, with the traceability table | both approve |
| ship (`/ship`) | version, changelog, release, deploy, smoke test | evidence of the release |

- **Your steps.** Approving the spec and confirming the release are board questions that only you can answer: the
  approval page (answer with what to change to send the work back) or a signed phone link. A yes in an agent's chat
  doesn't count.
- **Rework.** A failed check sends the work back with its feedback, and the checks nobody started yet are skipped
  (no review of a build that fails its tests). A stage sent back too many times asks you: another round, or stop.
- **Task cards.** `split_subtask(subtask_id, parts)` parts can carry `acceptance`, `covers` (AC ids), `inputs`
  (workspace paths or memory ids instead of pasted text), `output` and `criticality: low|high`. Splits go at most 3
  levels deep, 2 to 12 parts each.
- **Tiers.** Each card's difficulty x criticality picks the tier and effort: easy and low goes to a cheap model at low
  effort, hard and high to a top model at high effort. Agent daemons start their CLI on that model and effort
  (`COMMONS_MODEL`, `COMMONS_EFFORT` in the runner script).
- **Evidence.** Build, verify and ship steps only finish with a commit or diff, or a `TESTS:` / `EVIDENCE:` line.
- **One stage.** `stages=["verify"]` (or `/test`) runs just that part, with the task as its input.
- **See it.** The dashboard's Workflow page: the stage spine with your approvals, the agent tree with tiers, the
  traceability matrix (gaps in red) and tokens handed to agents against handing each one every upstream result.

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

`commons quota install --url <hub>/mcp --api-key-file <keyfile>` runs `commons quota sync` every 10 minutes on this machine (also a `commons setup` step) so models and the tool version stay current, with or without an aiusage database.

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
