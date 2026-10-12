# Auto-capture: lifecycle hooks and observations (v0.8)

Agents shouldn't have to remember to call memory tools. With hooks installed, Commons:

| When | What happens |
|---|---|
| **Session start** | injects a token-budgeted context pack: ready subtasks, inbox, leftovers ranked by your expertise, preferences, KB digest, and recent decisions/observations for the project (`COMMONS_HOOK_CONTEXT_TOKENS`, default 1200) |
| **Prompt submit** | records the prompt, and adds a short "possibly relevant memory" snippet *only* when there's a confident match |
| **After each tool call** | appends a compact observation (tool, input, output, error flag, file touched), fire-and-forget |
| **Stop / session end / pre-compact** | turns the session's raw observations and the transcript tail into **typed observations** |

Typed observations are `decision`, `bugfix`, `discovery`, `security_alert`, `todo`, `change` and `summary`. They are
stored as `observation` items (searchable, graphed, KB-indexed), and they fan out:

- `decision` goes to the reasoning log (`record_decision`).
- `todo` becomes a leftover, so another agent can pick it up.
- `security_alert` sends a notification (`COMMONS_NOTIFY_WEBHOOK`, dashboard).

When the provider-layer role `observer` is configured (`commons roles set observer <model>`), an LLM writes the
summary and typed observations. Otherwise rule-based heuristics do it, and they never fail.

## Install

```bash
commons hooks install claude-code      # ~/.claude/settings.json
commons hooks install codex            # ~/.codex/hooks.json   (trust them once via /hooks in Codex)
commons hooks install gemini           # ~/.gemini/settings.json
commons hooks install cursor           # ~/.cursor/hooks.json
commons hooks install all --command /opt/commons/bin/commons   # absolute path if commons isn't on PATH
commons hooks uninstall cursor
```

Installing is idempotent. Your other hooks and settings are preserved, and a `.bak` copy of the original file is
written first.

| Harness | Events wired |
|---|---|
| Claude Code | SessionStart, UserPromptSubmit, PostToolUse, Stop, SessionEnd, PreCompact |
| Codex CLI | SessionStart, UserPromptSubmit, PostToolUse, Stop, SessionEnd, PreCompact |
| Gemini CLI | SessionStart, BeforeAgent, AfterTool, AfterAgent, SessionEnd, PreCompress |
| Cursor | sessionStart, beforeSubmitPrompt, postToolUse, afterFileEdit, stop, sessionEnd, preCompact |

Context injection uses each harness's documented output: `hookSpecificOutput.additionalContext` for Claude Code,
Codex and Gemini, and `additional_context` on `sessionStart` for Cursor. Cursor's `beforeSubmitPrompt` can't
inject context, so it only records the prompt.

## Where hooks send data

- `COMMONS_URL=https://commons.example.ts.net` posts to `/agent/hook` with the agent key: `COMMONS_API_KEY`, or
  `~/.commons/agents/$COMMONS_AGENT.key` as written by `commons connect`.
- Without `COMMONS_URL`, hooks write straight to the local database (`COMMONS_DB`).

Hooks **never block the agent**. They time out after `COMMONS_HOOK_TIMEOUT` (default 2 s), always exit 0, and
when the server is unreachable they append the event to `~/.commons/hook-queue.jsonl`. The queue drains on the
next successful call.

## Privacy

- Anything inside `<private>…</private>` is stripped **on the client** before it is sent, and again on the server.
- Secrets are redacted by the same detector used everywhere else, including high-entropy strings.
- Per project: `commons capture <project> --enabled off`, or `--tools off`, `--prompts off`, `--inject off`.
  The MCP tool is `capture_settings`. Set `COMMONS_CAPTURE=0` to disable capture globally.

## Manual observations and the stream

- Agents without hooks can call `observe(kind, text, project, why)`.
- `commons observations --project X` (or the MCP tool `memory_stream`) shows the live memory stream.
