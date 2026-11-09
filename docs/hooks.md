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
A session start the hub received but did not answer in time is held for that session instead: its next prompt asks
the hub for the pack once, with whatever is left of the hook's 4-second budget as its timeout, and the pack arrives
with that prompt, after the prompt's own duplicate-work warning, recall and claim id, so a cut to fit trims only the
pack. The session is marked as asked before that request goes out, so a hook the harness kills while it waits never
leaves the next prompt to ask again, and a hub that could not be reached at all is not asked again. A session start,
and a prompt, wait for the hub only for what is left of that budget, less 0.7 s for the interpreter's own start.

## Canon on this machine

Canon (product facts, internal state, company rules; see [tools.md](tools.md)) reaches a session before any hub call,
so a slow or unreachable hub costs it nothing:

- **The cache.** A detached `commons canon pull` (at session start, at most once a minute, and at most every 5 minutes
  on prompts) writes `~/.commons/state/canon.json`, the hub's `canon()` plus `fetched_at`, `ok` and `error`, and
  `canon.md`, a compact rendering: the CANON line with the precedence, the company core, one short card per product.
  It keeps the changes of the last 7 days, at most 500, each old and new value cut to 400 characters (the lines show
  120), so a week of big lists still fits; a Canon too big for the cache's 2 MB is a failed pull, said plainly. A
  failed pull keeps the old cache and records `ok: false` with the error (never the key). The pull runs from your home
  folder with Python's safe path (`-P`), never from the session's project. Peers and guards read the same files;
  nothing here writes back to Commons. Without `COMMONS_URL` the pull reads the local database, in the hook itself.
- **Session start** (every source: startup, resume, clear, compact) opens with the core, the card of the product whose
  `work:<key>.repos` holds this checkout's `origin` remote (its one line when the card does not fit, one line each for
  any other product of that repo, and outside any product's repo one line per product), the last 7 days of changes to
  every product and the company (this checkout's product first, as many of the newest 8 as fit after the core and the
  card; the next message shows up to 10 more), the open conflict count and, from the refs of the last fetch (no
  network), how many commits the checkout is behind origin's default branch. A cut core ends with where the rest is. A
  cache that is missing, more than 6 hours old or failing is said in one plain line, which Claude Code also shows you.
  The hub's pack follows, without Canon a second time; only a fresh cache whose last pull worked, once the products
  made it in, lets the hub leave its own FACTS out. A prompt from a session whose start never reached the hook gets the
  start's Canon.
- **Each prompt** adds one line per Canon change this session has not been shown yet
  (`~/.commons/state/sessions/<session_id>.json`, written once the hook's output is ready), the newest 10, then one
  line naming `changes(since=...)` for any older ones, which then count as delivered (a burst of changes reaches a
  session once), and the card of each product the prompt names (its key, or a `name`, `names`, `alias`, `aliases` or
  `name_*` fact, e.g. `name_zh`), once per session; a name found inside a longer product name ("Pulse" in "Pulse
  Search") counts once it also stands on its own. The first 16 KB of a prompt are read; `_` and `-` are part of a
  word; a key that is an everyday word (secret, journal, poise, santa, cooli) counts only through a name fact. Names in
  scripts without spaces, such as Chinese, match anywhere in the prompt.
- **Instruction files.** A second managed block, `<!-- commons:canon:begin ... -->` to `<!-- commons:canon:end -->`,
  in `~/.claude/CLAUDE.md`, `~/.grok/AGENTS.md`, `~/.gemini/GEMINI.md` and `~/.codex/AGENTS.md` wherever the Commons
  playbook is installed, tells sub-agents, peers and the woken Claude where Canon is (`canon()` on the hub, a copy in
  `canon.md`), dated by the pull, with the precedence and the owner-only list. It holds no text an agent wrote:
  `record_decision` takes any agent's word for the owner's, so the standing rules and the focus stay in `canon()` and
  the session's own context, the owner-only list is shown as `<kind>.<predicate>` names only, and a precedence Canon
  changed is a pointer to `canon()`. `<` and `>` become `‹` and `›`, so nothing forges a marker; the markers count only
  on lines of their own (spaces, tabs and a byte order mark around them are fine), and a begin marker whose end was
  deleted is dropped by itself, so a later write never takes the text that follows it. Each write is locked, atomic (a
  symlinked file stays a symlink; a hard-linked one is written in place) and keeps the file's line endings; a
  read-only file, or one that is not UTF-8, is left alone and never stops the others. Nothing outside the markers
  changes; the block goes when the cache is more than 3 days old or Canon is turned off, and `commons instructions
  uninstall` removes it.
- `COMMONS_HOOK_CANON=0` turns all of this off on a machine. The hook's whole context stays under 9,500 characters
  (Claude Code moves a longer one to a file): Canon's part stays under 6,000, so the hub's busy and duplicate-work
  warnings, recall and the claim id always keep room, and the hub's pack is cut to fit. Each Canon line stays one
  line: a subject, product key or agent name prints with any line break as a space, so none forges a line.
- **Upgrade the hub before the hook clients.** A hub older than v0.38 has no `canon` tool: the pull records that
  quietly (no notice at every session start) and the hub keeps sending its own pack.

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
