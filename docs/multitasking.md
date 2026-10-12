# Multitasking, routing and mission control (v0.7)

## Concurrent scheduling
Many projects and jobs run at once. Each claim picks the best pending subtask by
`job.priority*10 + project.priority*5 + subtask.priority − in_flight(project)/weight` (fair share across projects).
- Per-project: `commons sched project NAME --priority N --weight W --max-concurrent K --budget USD --pause/--unpause`.
- Per-job: `commons sched job JOB --priority N --after JOB1 JOB2` (cross-project dependencies; cycles rejected).
- Per-agent capacity: profile `max_concurrent` (`commons connect --max-concurrent`); agents work several subtasks in parallel.
- Preemption: `commons sched preempt --below N [--requeue]` pauses lower-priority jobs (optionally handing back in-flight
  subtasks); `commons sched resume [JOB]`.
- Isolation: context packets, Drive and ACLs are per project.
- Claims are atomic (`UPDATE … WHERE status='pending'`), so there are no duplicate claims across processes.
  `tests/test_v07_drive_sched.py::test_stress_5_projects_concurrent_agents` runs 5 projects × 3 jobs with 6 agents in
  separate threads/connections and checks every subtask is claimed exactly once and every job finishes.
- **Mission control** (dashboard, `commons mission`, MCP `mission_control`): all active projects/jobs/agents, queue depth,
  in-flight vs limits, load vs capacity, throughput per hour, cost.

## Execution targets (agent + model + effort)
Each subtask is routed to an agent, a model and an effort level (`minimal|low|medium|high|max` → thinking budget /
reasoning effort / max tokens / temperature / tool access). The policy table maps task type → effort + tier
(e.g. architecture/red_team → high/top, summarize/format → low/cheap, code → medium/coding, review → different model family
than the author). Difficulty is estimated (heuristic, or the planner model when configured) and shifts the level.
Failures and rejected reviews escalate effort/model; repeated cheap successes de-escalate (learned per task type).
Mode: `quality-first | balanced | cost-first` globally (`commons route mode`) or per job budget.
Provider mapping: Anthropic `thinking.budget_tokens`, OpenAI `reasoning_effort` (o-series/gpt-5), xAI `reasoning_effort`
(grok-3-mini), Gemini `thinkingConfig.thinkingBudget`, OpenRouter `reasoning.effort`, Ollama `think`.
CLI workers: `claude --model` + `MAX_THINKING_TOKENS`, `codex -m … -c model_reasoning_effort=…`, `cursor-agent --model`,
`gemini -m`. Every decision (target, effort, rationale, alternatives, estimated and actual cost, outcome) is stored:
`commons route why SUBTASK`, MCP `route_why`, dashboard **Routing** (editable policy, cost/quality per tier).

## Task breakdown
Dashboard **Task breakdown** / `commons breakdown JOB` / MCP `job_breakdown`: the planning step, the DAG (dependencies and
review gates), and per subtask the agent, model, effort, rationale, status timeline, attempts/escalations, reviews,
tokens/cost, outputs and Drive artifacts.
