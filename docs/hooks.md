# Auto-capture: lifecycle hooks and observations (v0.8)

Agents shouldn't have to remember to call memory tools. With hooks installed, Commons:

| When | What happens |
|---|---|
| **Session start** | puts Canon first, from this machine's cache ([below](#canon-on-this-machine)), then injects a token-budgeted context pack: ready subtasks, inbox, leftovers ranked by your expertise, preferences, KB digest, and recent decisions/observations for the project (`COMMONS_HOOK_CONTEXT_TOKENS`, default 1200) |
| **Prompt submit** | stores the prompt word for word as a searchable memory, and adds a short "possibly relevant memory" snippet *only* when there's a confident match; adds one line per Canon change since the session last heard, and the card of a product the prompt names |
| **After each tool call** | appends a compact observation (tool, input, output, error flag, file touched), fire-and-forget |
| **Stop / session end / pre-compact** | stores each new agent reply word for word, then turns the session's raw observations and the transcript tail into **typed observations** on top |

Every message is kept, not only what a model judged worth keeping: the prompt and reply rows carry the tag
`conversation`, share the session id, and `search(context=N)` returns the turns around any short hit (up to 320 characters; longer turns stand alone). Each message is
stored once per session even though every Stop resends the transcript tail. `--prompts off` turns verbatim capture off.

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
when the server can't be reached they append the event to `~/.commons/hook-queue.jsonl`. Each successful hook then sends
up to 25 queued events, reading the file a line at a time. One hook holds a batch at a time, corrupt lines and events
the hub refuses are dropped, and the queue stops growing at `COMMONS_HOOK_QUEUE_MAX_MB` (default 50). A slow reply is not an outage: the server already has that event, so it is not queued.
A session start the hub does not answer (slow or down) is held for that session instead: its next prompt asks the hub
for the pack again, and the pack arrives with that prompt.

## Canon on this machine

Canon (product facts, internal state, company rules; see [tools.md](tools.md)) reaches a session before any hub call,
so a slow or unreachable hub costs it nothing:

- **The cache.** A detached `commons canon pull` (at session start, at most once a minute, and at most every 5 minutes
  on prompts) writes `~/.commons/state/canon.json`, the hub's `canon()` plus `fetched_at`, `ok` and `error`, and
  `canon.md`, a compact rendering: the CANON line with the precedence, the company core, one short card per product.
  A failed pull keeps the old cache and records `ok: false` with the error. Peers and guards read the same files;
  nothing here writes back to Commons. Without `COMMONS_URL` the pull reads the local database, in the hook itself.
- **Session start** (every source: startup, resume, clear, compact) opens with the core, the card of the product whose
  `work:<key>.repos` holds this checkout's `origin` remote, the last 7 days of changes, the open conflict count and,
  from the refs of the last fetch (no network), how many commits the checkout is behind origin's default branch. A
  cache that is missing, more than 6 hours old or failing is said in one plain line, which Claude Code also shows you.
  The hub's pack follows, without Canon a second time.
- **Each prompt** adds one line per Canon change since the session's watermark
  (`~/.commons/state/sessions/<session_id>.json`), then moves it, and the card of each product the prompt names
  (its key, or a `name`, `names`, `alias`, `aliases` or `name_*` fact, e.g. `name_zh`), once per session. Names in
  scripts without spaces, such as Chinese, match anywhere in the prompt.
- **Instruction files.** The same core, at most 20 lines, is kept in a second managed block,
  `<!-- commons:canon:begin ... -->` to `<!-- commons:canon:end -->`, in `~/.claude/CLAUDE.md`, `~/.grok/AGENTS.md`,
  `~/.gemini/GEMINI.md` and `~/.codex/AGENTS.md` wherever the Commons playbook is installed, so sub-agents, peers and
  the woken Claude carry it too. Nothing outside the markers changes; `commons instructions uninstall` removes it.
- `COMMONS_HOOK_CANON=0` turns all of this off on a machine. The hook's whole context stays under 9,500 characters
  (Claude Code moves a longer one to a file): Canon goes in whole and the hub's pack is cut to fit.

## Code changes

When a session starts inside a git repo, the hook notes the commit it began at. When the agent stops, it sends what
the session changed: commits made since then plus anything uncommitted, the diffstat, new untracked file names, and
the repo and branch. Commons keeps it with the work in hand: the subtask the agent holds (in the job's workspace,
`/jobs/<job>/<part>/changes.diff`), else its open `start_work` claim (`/work/<claim>/`), else the session
(`/sessions/<id>/`). The Drive redacts secrets, keeps every version and skips unchanged diffs, so resending at every
stop costs nothing. The dashboard shows it on the job's timeline and on the Changes page. Turn it off per machine
with `COMMONS_HOOK_GIT=0`, or for everyone with "Keep each agent's code changes" on the Setup page.

## Privacy

- Anything inside `<private>…</private>` is stripped **on the client** before it is sent, and again on the server.
- Secrets are redacted by the same detector used everywhere else, including high-entropy strings.
- Per project: `commons capture <project> --enabled off`, or `--tools off`, `--prompts off`, `--inject off`.
  The MCP tool is `capture_settings`. Set `COMMONS_CAPTURE=0` to disable capture globally.

## Manual observations and the stream

- Chat apps with no hook API (claude.ai, Grok, ChatGPT connectors) call `turn(user_message, reply=<previous reply>)`
  at the start of every turn. It does what the prompt and Stop hooks do: stores both messages verbatim, distills
  typed observations, and returns the relevant memory before the agent answers.
- Agents without hooks can also call `observe(kind, text, project, why)`.
- Keep-working (Claude Code, opt-in): with `COMMONS_HOOK_KEEP_WORKING=1` in the hook's environment, a Stop is
  blocked while Commons has tasks waiting for the agent and its quota allows, at most `COMMONS_HOOK_KEEP_WORKING_MAX`
  (default 5) times in a row per session.
- `commons observations --project X` (or the MCP tool `memory_stream`) shows the live memory stream.
