# Workers: running orchestrated subtasks headlessly

Most AI agents only run while someone is chatting with them. `commons worker` gives an agent a **seat** that keeps
polling Commons and runs its CLI non-interactively for every subtask assigned to it.

```bash
commons connect claude-code --platform claude-code --expertise code:0.9,plan:0.8 --no-install
commons worker --agent claude-code --preset claude --model claude-sonnet
```

| Preset | Command | Notes |
|---|---|---|
| `claude` | `claude -p --output-format text` | Claude Code print mode; add `--mcp-config` / `--allowedTools` to taste |
| `codex` | `codex exec --skip-git-repo-check -` | Codex CLI reading the prompt from stdin |
| `cursor` | `cursor-agent -p --output-format text` | Cursor CLI agent |
| `gemini` | `gemini -p "$(cat {prompt_file})"` | Gemini CLI |
| `echo` | `cat` | debugging |

Any other agent: `--cmd "<shell command>"`. The prompt arrives on **stdin** and in `{prompt_file}`. The environment has
`COMMONS_SUBTASK_ID`, `COMMONS_JOB_ID`, `COMMONS_TASK_TYPE` and `COMMONS_PROMPT_FILE`. Example for an API-only model (Grok via
xAI's OpenAI-compatible API, using the `llm` CLI):

```bash
commons worker --agent grok --model grok-4 --cmd "llm -m grok-4"
```

## Output protocol
stdout is the result. Optional lines:

```
VERDICT: approve | reject      # required for review / red_team / test / vote subtasks
LEFTOVER: <unfinished item>    # carried forward and picked up later
PREFERENCE: <user preference>  # learned and injected into future tasks
TOKENS: <n>                    # output tokens, if your CLI reports them (otherwise estimated)
```

A non-zero exit code marks the attempt failed. The subtask is retried on another agent until `max_attempts`.

## Reliability
- The worker heartbeats every lease/3 seconds while the command runs. If the worker dies, the lease expires, the
  scheduler or any claim reaps it, and the subtask is reassigned to a different agent.
- `--timeout` kills runaway commands. `--idle-exit N` exits after N idle seconds (useful from cron or CI).
- Remote hub: `--url http://hub:8765/mcp --api-key $(cat ~/.commons/agents/grok.key)`.
- Run the scheduler somewhere: `commons scheduler` (loop) or `*/5 * * * * commons scheduler --once` in system cron.

## Safety
Workers execute real CLIs with whatever permissions you give them. Run them in a sandbox, container or limited account,
restrict the CLI's tool permissions, and set budgets (`--max-cost` on submit, `COMMONS_DAILY_COST_CAP`). Upstream
agent outputs are fenced as `<untrusted-agent-output>` in prompts, but prompt injection cannot be fully prevented.
See ../FLAWS_AND_GAPS.md.
