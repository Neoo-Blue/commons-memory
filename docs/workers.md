# Workers: running orchestrated subtasks headlessly

Most AI agents only run while someone is chatting with them. `commons worker` gives an agent a **seat** that keeps
polling Commons and runs its CLI non-interactively for every subtask assigned to it.

```bash
commons connect claude-code --platform claude-code --expertise code:0.9,plan:0.8 --no-install
commons worker --agent claude-code --preset claude --model claude-sonnet
```

Presets were checked on 2026-10-11 against each vendor's own CLI reference. The CLIs change often, so run
`<cli> --help` if a flag is rejected.

| Preset | Command | Verified against | Notes |
|---|---|---|---|
| `claude` | `claude -p --output-format text --permission-prompts none "<query>"` (prompt on stdin) | [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference) | `-p` is print mode; piped stdin is the content. `--permission-prompts none` (v2.1.259+) denies prompts nobody can answer instead of hanging; drop it on older versions. Add `--allowedTools` / `--mcp-config` / `--max-budget-usd` as needed. Ran end-to-end on 2026-10-13. |
| `codex` | `codex exec --skip-git-repo-check --sandbox workspace-write -` | [Codex CLI reference](https://developers.openai.com/codex/cli/reference) | `exec` is non-interactive; `-` reads the prompt from stdin; the sandbox keeps edits in the workspace. |
| `cursor` | `"$(command -v cursor-agent \|\| command -v agent)" -p --output-format text --trust "$(cat {prompt_file})"` | [Cursor CLI parameters](https://cursor.com/docs/cli/reference/parameters) | The binary is now `agent`, but Grok Build also installs an `agent`, so `cursor-agent` (still shipped) is tried first; `--trust` skips the workspace-trust prompt (headless only). No stdin prompt is documented, so the prompt goes in as an argument. Add `--force` only in a sandbox. |
| `grok` | `grok -p "$(cat {prompt_file})" --output-format plain --always-approve` | `grok --help` (Grok Build 1.0.52) | Single-turn run that auto-approves its own tool calls; run it in a worktree or sandbox. Ran end-to-end on 2026-10-13. |
| `cursor-legacy` | `cursor-agent -p --output-format text "$(cat {prompt_file})"` | older Cursor installs | Same flags, older binary name. |
| `gemini` | `gemini --output-format text -p "$(cat {prompt_file})"` | [Gemini CLI headless mode](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/headless.md) | `-p` or non-TTY stdin triggers headless mode. Exit code 53 means the turn limit was exceeded. |
| `echo` | `cat` | n/a | debugging |

Not verified by running: the box that built this release has none of these CLIs logged in. The flags come from the
docs above, and the worker itself is tested with mock commands.

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
