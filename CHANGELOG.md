# Changelog

All notable changes, newest first. The README describes the current release only; deep dives live in [docs/](docs/).

## v0.12.1: Discover shows each skill once
- Mirror copies of the same skill inside one repo (one folder per AI tool) collapse into a single chip; an imported copy
  wins.

## v0.12.0: every AI knows the routine by heart
- **`commons instructions install all`** writes the Commons master instructions into each installed AI's
  always-loaded instructions: Claude Code (`CLAUDE.md` plus an always-relevant `commons` skill), Codex (`AGENTS.md`),
  Gemini CLI and Antigravity (`GEMINI.md`), Grok Build (`~/.grok/AGENTS.md`), Cursor (an `alwaysApply` rule). The
  block is marked, so installing again updates it in place and `uninstall` removes only it; originals are backed up.
- **The playbook reaches agents that only poll their mail:** `msg_recv` delivers it once per version, as `pickup`
  and `turn` already did.
- **Agents hand everything over.** `handover_status()` lists what Commons still lacks from an agent (profile and what
  it can reach, memories, keys and passwords, scheduled tasks, usage windows, current work, live activity), with the
  tool for each; `pickup()` names the missing items until it's complete. `vault_deposit` lets an agent hand over a key
  it holds (encrypted, never replacing another's); `set_profile(reach=[...])` records what it can access.
- **A task handed to another agent is tracked work.** `msg_send(type="task")` creates a job that shows as running, goes
  to the recipient if it is live and otherwise to the best live agent, and reports its result back to the sender.
  Every finished job now tells its submitter.
- **Discover groups by repo**: one row per repository with each of its skills, rules and agent files as a chip,
  instead of the same repo repeated on many rows.
- **Cleaner capture**: diff, code, table and shell lines in a transcript are no longer recorded as decisions.
- **The vault's decision agent sees what the user said** to the requesting agent in the last two hours (captured
  verbatim), so a write the user asked for in chat counts as user-backed.
- **Hand-offs show what Commons already knows.** A direct `msg_send` returns the relevant memory (`commons_knows`)
  so the sender can often do the work itself instead of waiting on another agent.

## v0.11.1: Basic auth in the vault
- The vault can attach a key as HTTP Basic auth (`--auth basic:USER`, the stored value is the password), for web UIs
  and WebDAV.

## v0.11.0: Commons is the vault
- **Credential vault with a decision agent.** Commons stores the user's API keys itself, encrypted (AES-GCM, bound to
  each name) with a master key kept outside the database. Agents list them with `credentials()` and use one with
  `use_credential(credential, url, justification)`: Commons makes the call and returns only the response, so no agent
  ever sees a key. A key is only ever sent to its own service's URLs, and responses are scrubbed of it.
- **Case-by-case decisions.** The decision agent (TypeSafe, or an LLM `gatekeeper` role) judges each request against
  the agent's reason, the user's asks on the board and the agent's current work. Sound reads get a one-hour grant;
  writes need the user's request behind them and cover exactly one call; anything destructive, costly or that messages
  people goes to the user under "Needs you", and a "yes" there grants that one call. With no judge available it fails
  closed. Every decision is logged (`commons vault decisions`).
- **Admin CLI:** `commons vault set NAME --service S --base-url URL --auth header:X-Api-Key < value`, `list`, `rm`.
- **`msg_send` says whether the recipient is live** and, if not, which agents are, so work that can't wait isn't
  parked on an agent that only runs when its user opens it.

## v0.10.0: everything remembered, limits respected, nothing dropped
- **Every message is memory.** Capture hooks store each user message and each agent reply word for word (searchable,
  with `search(context=N)` returning the turns around a hit), and distilled decisions/todos come on top instead of
  replacing them. Count-only "Session: N prompt(s)" summaries are gone.
- **No duplicate work.** `start_work` checks a task against what other agents are doing now (work claims, claimed
  subtasks, overlapping paths) and what was finished in the last 30 days; busy means don't start, done means build on
  it. `finish_work` or `handoff` closes the claim with its result. Prompt hooks and `turn` run the same check on every
  user message and warn the agent without being asked.
- **Agents know the routine without being told.** Commons hands each agent its playbook on the first `pickup`/`turn`
  and again whenever it changes.
- **`turn` hook for chat apps** with no hook API (claude.ai, Grok, ChatGPT connectors): one call per turn stores the
  exchange verbatim and returns the relevant memory before the agent answers.
- **Quota-aware distribution.** Agents report their 5-hour and weekly windows (`quota_report`, or `commons quota sync`
  from aiusage). Commons learns your own usage pattern, keeps a reserve for you, routes work only to agents with
  headroom, learns each task type's cost, and parks work until the earliest reset when everyone is near the limit.
- **Pause and resume without losing progress.** `subtask_pause` and `quota_exhausted` save a checkpoint; the work
  comes back after the reset (or to another agent) with the checkpoint in its packet. The agent daemon detects 402/429
  and usage-limit errors and parks the task instead of retrying.
- **Scheduled tasks live in Commons.** `schedule_report` registers what an agent runs on a schedule (cron + timezone,
  how it works, the acceptance bar) and `schedule_ran` reports each run. A missed run, or an owner out of quota past
  the grace window, is taken over by another agent with the instructions, acceptance bar and last good output.
  Missed-run takeovers start once the owner has reported a run, and the taker first checks whether the run already
  happened, so side effects (emails, posts) are never repeated.
- **Capacity page** in the dashboard: every agent's windows, your reserve, reset timers, and the schedules table.
- **Ask-Me questions.** `board_add(kind=question)` puts a question for you under "Needs you" on the Board, with what it
  holds up; your answer goes straight to the asking agent's inbox.
- **Keep-working Stop hook** (opt-in, `COMMONS_HOOK_KEEP_WORKING=1`): Claude Code keeps going while Commons has tasks
  for it and quota allows, bounded per session.
- **v0.9.3 merged:** wake prompts typed into a terminal (tmux/PTY) now carry only event types and sender names,
  never message text (any agent with a key could otherwise inject instructions into the user's session).
- **Fixes:** imports no longer accept blank memories (every writer now refuses empty content; `text` works as an alias
  for `content`); agents can `forget` memories they wrote; relayed subagent reports are no longer captured as
  user prompts; task packets kept the related memory (it was overwritten).

## v0.9.3: instant wake
- **`commons wake daemon`**: holds the agent's SSE wake stream open with its agent key and, within a second of an
  event, runs a command (`--exec`, events JSON on stdin), types into a tmux pane (`--tmux`), or injects into an agent
  CLI it runs in a PTY (`--pty 'claude'`), the PTY instant wake for Claude Code, Codex, Gemini and Cursor CLI.
  Standard library only; dials out over plain LAN/tailnet HTTP, nothing exposed.
- **Every event that targets an agent now wakes it**: direct messages, task readiness, task assignment
  (`task_assigned`), lease expiry (`lease_expired`), debate turns (`debate_turn`), and board `@mentions`
  (`board_mention`) on add and on edits.
- **Missed-event replay**: `/agent/stream` and `/agent/ws` replay events since `Last-Event-ID` / `?since=` or the
  agent's last ack (`POST /agent/wake/ack`, WS `{"op":"wake_ack"}`, `GET /agent/wake/replay`). The daemon reconnects
  with backoff after a server restart and acks after each successful wake.
- **Coalescing**: the daemon debounces bursts into one wake (`--debounce`, default 1 s); push channels skip a new
  delivery while one queued in the last `COMMONS_WAKE_DEBOUNCE` seconds (default 2) is still pending.
- **Presence `wake_mode`**: `instant` (holds a stream), `long-poll`, `push` (wake channel), `polling`, `offline`, in
  `presence`, `commons presence` and the dashboard.
- SSE stream delivers within ~50 ms of publish (was a 250 ms tick).
- Connect page and `commons connect` emit a wake setup block for every platform.

## v0.9.2: capture ignores harness notices
- Background-task notices that a harness relays through the prompt hook (`<task-notification>`, system reminders) are
  no longer recorded as user prompts or used to trigger a recall search.

## v0.9.1: quieter capture
- Capture hooks no longer store a "Session: 0 prompt(s), 0 tool call(s)" summary on turns where nothing happened,
  and a line captured from the transcript tail is stored once per session (consecutive Stop events resend the same
  tail, which tripled heuristic "decisions").

## v0.9.0: lean, fast, instant, and proven on your own data
- **Beats mem0 on LoCoMo QA in every category** (78.8% vs 70.3%, same reader and judge, p = 1e-11) with no LLM
  at write time: `search(context=N)` returns the turns around each hit, and relative dates resolve against each
  memory's own date at read time.
- **Project board** (`board`, `board_add`, `board_update`): verbatim asks, targets, expectations, actions and
  deliveries per project and layer; evidence-gated done; repeat counting; first in every pickup and session start;
  a Board page in the dashboard.
- **Dashboard refresh**: one accent on neutral charcoal (no gradient), Geist, tabular figures, gzip, focus kept across
  live re-renders, fixed state/element-id collisions (Board TypeError, Memory searching for an <input>).
- **Lighter and steadier hub**: one maintenance loop per database, CAS job claims, KB clustering 70x faster, cached
  sentence splitting, batch-8 embedding, imports embed only their own rows, remote calls time out.
- **Measured against the rivals, here.** mem0 OSS, MemPalace and claude-mem were run on the same LoCoMo data:
  QA ties mem0 (69.5 vs 70.3, p = 0.45) with no LLM per write and 6-15x faster search; retrieval beats MemPalace
  (0.708 vs 0.545 dialog-level, its own metric) and claude-mem (0.496). `benchmarks/live_agents.py` checks real
  agents end to end; `bench scale --target postgresql://` compares backends (SQLite wins 4-10x).
- **Race and load fixes** from a threaded test suite and a py-spy profile of the live hub: cross-process
  `complete_subtask` writes side effects only for the winner; one reindex at a time; per-thread `defer_embed`;
  no embedding under the store lock; one query to hydrate search hits; `synchronous=NORMAL`; librarian dedupe is
  new-items x pool. `handoff` returns `session_id`.
- **TypeSafe contradictions.** With `TYPESAFE_API_KEY` set, the librarian flags new notes that contradict their
  nearest neighbours as open conflicts.
- **Lean tool surface.** Clients list a 21-tool core profile (2,970 tokens of schema, was 148 tools = 33,057
  tokens in every session). `tools(query)` searches the rest and `call(tool, args)` runs any of them. Profiles per
  connection (`?tools=` on the MCP URL), per agent (`commons agent tools NAME core|all|core,name`) or globally
  (`COMMONS_TOOLS`). Schemas are compacted and header-authenticated clients don't see `agent`/`api_key`.
- **Better ranking.** Score fusion replaces reciprocal-rank fusion by default (RRF flattened scores so the recency
  and entity boosts overrode relevance). LoCoMo turn any@1 31.2 -> 39.4, any@10 71.1 -> 76.5, session any@10
  95.8 -> 97.4, with no weight tuning. `search(as_of=)` now hides items written after that time; listings report
  true totals; `rerank=false` really turns reranking off.
- **20x faster search at scale.** Vector search is one numpy matrix multiply over a cached, self-invalidating
  matrix, and keyword search ranks inside FTS before filtering. 30k items: p50 1,383 ms -> 68 ms; 60k items: 114 ms.
  `commons bench scale` measures latency, throughput, DB size and memory by store size.
- **The hub no longer freezes.** Sync tools ran on the event loop, so one slow call (an import batch, a 25 s
  `msg_recv` long-poll) blocked every agent (`whoami` took 130 s under load; now 0.3 s). They run in worker threads.
- **Instant wake.** Assignments and delegations land in the agent's mailbox, so a `commons agent daemon` starts
  the agent CLI within a second and chat agents see them in `msg_recv`. New `summon(agents, reason)` tool wakes
  agents and reports who is ready, with a fix for each unreachable one. The daemon works from the CLI again (`--cmd`
  silently disabled the command since v0.7), gains `--preset claude|codex|gemini|cursor`, `--api-key-file`,
  `--parallel`, and `--install` (login service); its launchd unit used to start a second server.
- **Import everything, optionally curated by an AI manager.** `commons import claude-mem|mem0|claude-memory|sesame
  [--curate] [--url HUB]` is idempotent and keeps timestamps and projects; Claude Code `feedback` memories become
  shared preferences; sesame contributes service metadata and env-var names only. With `--curate` a model (the
  `curator` role, any provider) keeps/drops/merges each item, rewrites a self-contained title and summary, pulls out
  preferences, and holds uncertain items in the review queue, under a hard `--budget` with a dry-run estimate.
- **Is it doing its job?** `commons bench self` samples your own memories, has a model write paraphrased questions,
  and measures hit@1/5/10 and MRR on your real store. `commons bench qa-locomo|qa-longmemeval` measures end-to-end
  answer accuracy (reader + judge), the metric mem0 and Zep publish.
- **Graph Explorer v2.** Louvain communities, PageRank, Barnes-Hut force / radial / layered layouts, readable labels,
  search, focus, shortest path, upstream/downstream reach, minimap, saved views, PNG/SVG/JSON export, keyboard and
  mobile. Big graphs open as a cluster map (labelled by source file) and a cluster opens in place.
- **Dashboard.** Memory is a workspace: live filter and chips, inline edit, delete with undo, multi-select bulk
  tag/move/delete, keyboard j/k/e/x. Never-seen agents say so (no more "idle 20738d ago"), activity hides plumbing
  events, and several crashes and escaping bugs are fixed.
- **Security.** ACLs on every read path (graph, expand on any id, KB, memory_stream, facts, timeline_around,
  get_task, why); `export_graph(path=)` and `index_code` need the local owner or `COMMONS_ADMIN_AGENTS`; a
  tokenless dashboard refuses unknown Host names (DNS rebinding, `COMMONS_DASHBOARD_HOSTS`); orchestrator lease
  races (heartbeat, reaper, complete) and inbox/message claim races fixed; host-mount symlink escapes, email
  wake-channel SSRF and A2A task hijacking closed.
- **Fixes.** `handoff` works without `session_start`; reads never create projects; re-indexing keeps memory->code
  links and is atomic; edits keep the keyword index and vectors in sync; `commons status` sees the HTTP bind (also
  the Docker healthcheck); the Docker model cache is readable by the runtime user; Claude 4.6+/5.x get
  `output_config.effort` instead of thinking budgets (400s); structured outputs in the provider layer; HTTPS works
  on python.org macOS builds; Cloudflare-fronted providers get a User-Agent.

## v0.8.1: Connect an agent
- **Dashboard → Connect** (Capabilities group, also in the ⌘K palette). Pick Claude Code, Codex CLI, Cursor, Gemini
  CLI, ChatGPT/other HTTP MCP clients, any AI, or Docker/self-host, and copy: the exact install command or
  JSON/TOML config, the universal prompt (URL, agent name, project and key filled in), the skill install command,
  and a one-line `curl` check that runs `initialize` + the new `whoami` tool and prints `OK`.
- The MCP URL is detected from the browser's host and the server's bind (`COMMONS_PUBLIC_URL` wins), and is editable.
- **Create agent key** (name, role read/write/admin, optional project ACL grant): shown once. Only allowed on
  authed dashboards (admin users) or tokenless dashboards bound to a private address with a private client, per the
  existing `_private_host` rules. The new agent's *last seen* updates live as it connects.
- Troubleshooting checklist: 401 (key), unreachable (Tailscale `--accept-routes`/subnet routes, firewall, wrong
  IP), stale tool schema (restart the client), and wake channels/pollers so the agent receives work.
- **CLI**: `commons connect <platform> [--name N] [--new-key] [--role] [--project] [--url] [--json]` prints the same
  output. `commons connect <agent-name>` (anything that is not a platform id, or with `--platform`) keeps the old
  register-and-install behaviour. **JSON API**: `GET /api/connect`, `GET /api/connect_status?name=`,
  `POST /api/connect/key`.
- New MCP tool `whoami`. New env vars `COMMONS_MCP_HOST`/`COMMONS_MCP_PORT` (set by `commons serve`).
- README screenshots gallery (docs/screenshots/), captured from demo data.

## v0.8.0: retrieval quality and proof, plus zero-effort capture
- **Better default embedder.** `COMMONS_EMBEDDER=auto` uses fastembed (bge-small, with query instructions) when it
  is installed. Installers and the Docker image now include it, and the image bakes the model in. Otherwise it uses
  any OpenAI-compatible `/v1/embeddings` endpoint (`COMMONS_EMBED_URL`), falling back to hash. There is a
  `commons reindex` command and a background reindex job. Vectors are stored per model.
- **Ranking signals.** Weighted RRF plus entity boost (graph entities named in the query), time proximity ("last
  week", "in May", dates), recency decay and trust. `search(as_of=, explain=)` and the `Store.rank` API.
- **Optional rerank.** A local cross-encoder (fastembed) or an LLM (role `reranker`), budget-capped.
- **Benchmarks.** `commons bench longmemeval|locomo`, with per-question results committed. LongMemEval-S any@5 is
  97.7%; LoCoMo turn R@10 is 71.1% (75.1% with rerank) and session R@10 is 95.8%. See
  [benchmarks/README.md](benchmarks/README.md).
- **Lifecycle hooks.** `commons hooks install claude-code|codex|gemini|cursor`. Hooks inject context at session
  start, recall at prompt time, record tool observations and summarise at stop, session end and pre-compact. They
  never block, and they queue events while offline. See [docs/hooks.md](docs/hooks.md).
- **Observation pipeline.** Typed observations (decision, bugfix, discovery, security_alert, todo, change, summary)
  fan out to decisions, leftovers and notifications. Uses an LLM `observer` role or heuristics. `<private>` text is
  excluded, and capture can be switched off per project. New MCP tools: `observe`, `capture_settings`, `memory_stream`.
- **Three-step search.** `search_index`, then `timeline_around`, then `get_items`.
- **Also in this release:**
  - Temporal facts with validity windows (`fact_set`, `fact_get(as_of)`, `fact_history`, `fact_invalidate`).
  - `commons sweep`, a verbatim per-message store.
  - Scoped spaces (project, topic, person) and `memory_audit`.
  - Interactive HTML diagrams of the code graph and job plans, inspired by Archify.

  See [docs/knowledge.md](docs/knowledge.md).
- **Fixes.** Drive blob writes use a unique temp file per writer (concurrent identical uploads could race on one
  `.tmp` path). The v0.8 MCP tools and CLI commands are listed in [docs/tools.md](docs/tools.md).
- **Benchmark caveats.** Retrieval recall only (no end-to-end QA), CPU only, untuned weights, isolated per-question
  stores; the ranking signals cost about 3.6 points of LoCoMo turn R@1. Details in benchmarks/README.md.

## v0.7.2: production Docker image
- Multi-stage, non-root image with tini, a healthcheck and a `/data` volume; `*_FILE` env vars for Docker secrets;
  waits for Postgres; amd64 and arm64; published to GHCR by the CI workflow.
- Compose profiles for Postgres/pgvector, Tailscale, cloudflared and ngrok, plus `.env.example`. See
  [docs/docker.md](docs/docker.md).
- The dashboard runs tokenless on private binds (loopback, LAN, tailnet); `COMMONS_DASHBOARD_REQUIRE_TOKEN=1` forces
  a token anyway.
- Fix: a race in `ensure_agent` on a fresh database.

## v0.7.1 — README consolidation, one-line installers, universal prompt
- README rewritten around a 2-minute quickstart. Version history moved here, and deep docs moved to `docs/`.
- `install.sh` (`curl -fsSL … | sh`) and `install.ps1` (`irm … | iex`) one-line installers. They detect uv, then pipx, then Python, install from the GitHub release, and run `commons init`.
- [PROMPT.md](PROMPT.md): one paste-in prompt that connects any AI to Commons and teaches it the routine.
- [docs/configuration.md](docs/configuration.md): every `COMMONS_*` variable, generated from the code by `scripts/gen_env_reference.py`. A test keeps it current.
- Written as a Commons job (`job_01a12dc69b3070b6de9b76`, project `commons-readme`) by grok and muse, with cross-review and a debate.

## v0.7.0 —: native messaging + wake, routing to model/effort, Commons Drive, true multitasking

**No external bus needed.** Direct and broadcast messages (typed, threaded, at-least-once, long-poll), stored and graphed.
A `/bus/*` shim keeps existing agent-bus clients working. Per-agent wake channels: signed webhooks with retries and a
dead-letter queue, SSE/WebSocket streams, MCP notifications, ntfy, Pushover, Slack, Discord, Telegram, email, and local
command hooks. Presence (online/idle/sleeping/offline) with wake-on-demand before work is allocated, and
`commons agent daemon` for always-on CLI agents. SSRF-safe outbound allowlist; A2A push notifications. `commons bus import`
migrates history. See [docs/messaging.md](docs/messaging.md).

**Right model, right effort.** Every subtask gets an execution target (agent + model + effort), chosen from a
policy table that's editable and learns from outcomes, with escalation on failure. Each choice is explainable with
`route_why`. **Mission control**, **Task breakdown** and **Routing** dashboards. Concurrent multi-project scheduling with
priorities, fair share, limits, budgets, preemption and cross-project dependencies. See [docs/multitasking.md](docs/multitasking.md).

**Commons Drive.** A shared, versioned file store (history/diff/restore, locks, secret scanning, search, quotas,
ACLs, artifacts linked to tasks). Backends: local, S3/R2/MinIO/B2, WebDAV/Nextcloud, host folders/NFS/SMB mounts, SFTP, SMB,
FTP(S). Sync: git and rclone. See [docs/drive.md](docs/drive.md).

**Friendlier tools.** Every MCP tool accepts common aliases (`text`→`content`, `decision`→`what`, camelCase keys...).
Missing or unknown arguments produce one readable error that lists what the tool accepts. Project names are validated.

## v0.6.0 —: retro learning and remote access

**Learn from what you've already done.** `commons learn --dry-run` finds your Claude Code, Codex, Cursor, Gemini CLI, ChatGPT and Claude.ai history, your git and PR history, and your CLAUDE.md/AGENTS.md/.cursor/rules files. It then shows what Commons would learn and estimates the cost. `commons learn` extracts preferences, decisions with rationale, project facts, unfinished work (leftovers), model-by-task performance (which feeds routing) and procedures (skill drafts). Results go into a review queue that you approve in bulk on the dashboard's **Retro learning** page or with `commons learn approve`. Re-runs only read new history. See [docs/retro-learning.md](docs/retro-learning.md).

**Reach it from anywhere, safely.** `commons expose up tailscale | cloudflare | ngrok | ssh | bore | zerotier | wireguard`, plus `expose proxy caddy|nginx` and `expose service systemd|launchd|windows`. Public exposure is refused unless the server demonstrably requires keys. Cloudflare Access JWTs are validated. `commons status` and the dashboard show the URL and its health. Client snippets in `~/.commons/connect/` follow the reachable URL. Docker: `docker compose up -d`. See [docs/networking.md](docs/networking.md).

## v0.5.0 —: production hardening (Postgres, auth, ACLs, trust, escalation, A2A server)

v0.5 closes the open items from `FLAWS_AND_GAPS.md`. That file now separates what is fixed from what is still open and says why.

| Feature | How |
|---|---|
| Postgres + pgvector | `pip install commons-memory[postgres]`, `COMMONS_DB=postgresql://user@host/db`. Run `commons migrate-postgres DSN` to move an existing SQLite DB |
| Encryption at rest | `pip install commons-memory[encryption]`, `COMMONS_DB_KEY=...` (SQLCipher). For encrypted backups, `COMMONS_BACKUP_KEY=...` |
| HTTP auth for reads and writes | Agent API keys or OAuth 2.1 bearer tokens (`COMMONS_OAUTH_ISSUER`, `COMMONS_OAUTH_INTROSPECT_URL`). Rate limit with `COMMONS_RATE_PER_MIN` |
| Per-project ACLs | `commons acl grant PROJECT AGENT --role read|write|admin` |
| Trust scores | `commons trust list|set|unset`. Trust weights search and KB digests (`COMMONS_KB_MIN_TRUST`) |
| Nothing waits forever | Escalation ladder: reassign, then the distributor model, then notify you (`commons notifications`, `COMMONS_NOTIFY_WEBHOOK`) |
| Missed schedules | `schedule(..., catchup="skip|once|all", max_catchup=N)` |
| Background maintenance | In-process thread in `serve`/`dashboard` (`COMMONS_BACKGROUND=1|full|0`): KB rebuilds, librarian, escalation, retention, backups |
| Retention / backups | `commons prune`, `commons backup [--encrypt]`, `commons dump` / `commons import-memory` |
| A2A server | `POST /a2a` (send, stream, get, cancel, resubscribe), `/.well-known/agent-card.json`. Client: `commons a2a send URL "text" --wait` |
| Code graph | `pip install commons-memory[code]` turns on tree-sitter JS/TS. Call targets are resolved through imports and scope |
| Dashboard | `--tls-self-signed` or `--tls-cert/--tls-key`, SSE live updates, `commons dashboard-user add NAME --role viewer` |
| Secrets | Real Infisical adapter, `vault-dyn://` dynamic credentials revoked with the lease, entropy-based redaction |
| MCP hub | Pooled persistent upstream sessions, per-agent credentials, OAuth client-credentials/refresh for upstreams, `verify-catalog` |
| Tokens / tracing | Exact counts through tiktoken and the Anthropic/Gemini count APIs. OpenTelemetry with `[otel]` and `COMMONS_OTEL=1` |

Install everything with `pip install "commons-memory[all]"`. Workers: see `docs/workers.md` for the flags checked against each vendor's docs.

## v0.4.0 —: shared skill pool, research, model routing, org chart, librarian, MCP hub

| | |
|---|---|
| ![org chart](docs/img/dashboard-orgchart.png) | ![MCP hub](docs/img/dashboard-mcp.png) |
| ![skills](docs/img/dashboard-skills.png) | ![librarian](docs/img/dashboard-librarian.png) |

**Skill pool.** Commons is now the skill registry. Agents install only a tiny *bootstrap* skill and fetch everything
else on demand: `skill_list / skill_search / skill_get` (token-budgeted), `skill_publish / skill_update / skill_fork`,
`skill_rate`, `skill_rollback`, `skill_history` (full versions + diffs). Every version passes a gate before it can become
active: secret redaction, a prompt-injection / exfiltration scanner, then review by another agent, a configured
`skill_reviewer` model, or you (no self-review). Usage and success are tracked per agent and model; skills are graph
nodes (`version_of`, `used_by`, `authored_by`, `depends_on`, `forked_from`).

**Skill discovery and import.** `commons skills discover` (also run daily by `commons scheduler`) uses the GitHub REST
API, authenticated with a secret ref (`commons skills configure --token-ref env://GITHUB_TOKEN`), to find public
SKILL.md skills, Claude Code plugins and skill repos, AGENTS.md / Cursor-rules collections and awesome-lists. It
respects rate limits and does no HTML scraping. Results are ranked as **hot/trending** (star velocity over 1d/7d, from
daily snapshots) and **all-time popular** (stars and forks). They are stored as a catalog of metadata (source URL,
license, pinned commit), and nothing activates automatically. `commons skills add <id>` or the dashboard's
**Add to pool** imports the pinned version through the gate: a license allow-list, scanning, provenance, and a
mandatory review. Upstream changes show up as new *pending* versions with diffs. `commons skills import` moves skills
already on this machine into the pool, de-duplicating them: `~/.claude/skills`, project `.claude/skills`, Claude Code
plugins, `.cursor/rules`, `AGENTS.md`, `~/.codex` and custom paths. Add `--replace-local` to swap each local copy for a
pointer; the originals are backed up.

**Research.** `research(query)` searches and fetches with a pluggable provider (Brave, Tavily, Exa, SearXNG, or plain
`fetch` of URLs). Keys are secret refs. Pages are extracted to text, redacted and stored as cited `source` memories plus
a research note that feeds the project KB. Results are cached.

**Model providers and routing.** Connect Anthropic, OpenAI, OpenRouter, Google, xAI or a local Ollama with secret refs
only (`commons models add-provider openrouter openrouter --key-ref env://OPENROUTER_API_KEY`). The model registry
records context size, cost, capabilities, learned scores and which agents can run which models, and refreshes itself
from provider APIs to detect **new models**. You choose a **distributor** model (any id, e.g. an Opus-class model
direct or via OpenRouter; nothing is hard-coded) with `set_role_model("distributor", "<provider>/<model>")`. It plans
DAGs, builds the task-type → agent+model routing table, and when a new model appears it benchmarks it with cheap probes
under a $ budget, then proposes a reallocation (or applies it if auto-approve is on). The planner, preference
extractor, KB summarizer, skill reviewer and librarian use this layer when it's configured and heuristics otherwise.
The `@llm` worker mode runs API-only seats. A mock provider powers the tests and demo.

**Org chart.** `tree(root, depth, max_tokens)` and `commons tree` give a layer-by-layer outline with **stable index
paths** (`1.3.2`, `agents/2.1`) that agents can pass back to drill down. The layout is Workspace › Projects › (Jobs &
tasks, Workflows, Sessions, Knowledge base, Code dirs › files › classes › functions, Schedules › runs, Skills, Memories,
Leftovers), with alternate roots *agent › model › tasks* and *by skill*. It exports to Mermaid `flowchart TD` and to a
Markdown outline. The dashboard shows a tidy top-down tree with lazy-loaded, collapsible layers, child counts, hover
cards, breadcrumbs, search-jump-expand, zoom/pan with a minimap, and an outline mode.

**Librarian.** A built-in agent that keeps everything organized. It runs as a configured cheap model (role
`librarian`), as any connected agent claiming `librarian` subtasks (`librarian_run(delegate=true)`), or on heuristics
alone. Incrementally after writes and on the scheduler, within a $ budget, it:
- de-duplicates and merges near-duplicates
- auto-tags and assigns orphans to projects
- repairs graph links
- flags stale or contradicting items
- writes a one-line summary per tree node
- builds a "where to find X" index for `find(query)`, which returns the best few tree paths + ids for a few tokens
- tidies the skill pool

Every change is logged with provenance and can be undone. Merges go to a review queue unless auto-approve is on. Tools:
`librarian_run / status / proposals / approve / undo`, `find`.

**MCP hub.** Commons proxies your upstream MCP servers. Register each server once (stdio command or HTTP/SSE URL);
credentials are secret refs only. You can also import existing Claude Desktop / Cursor / Claude Code configs with
`commons mcp import`; literal keys are **not** copied but turned into `env://` refs. Agents see namespaced tools
(`github__create_issue`) through `mcp_search` (one line each), `mcp_describe` (one schema) and `mcp_call`, so only the
schemas they need cost tokens. On top of that, the hub adds:
- per-agent allow / deny / approve policies per server and tool (globs)
- risk tags (read / write / destructive), with destructive calls needing human approval by default
- per-agent rate limits
- an audit log of every call, with arguments redacted
- usage attribution
- health checks that reconnect or restart servers
- a cache for read-only results

Upstream output is redacted and wrapped in `<untrusted source="mcp:server/tool">` markers.

**MCP catalog.** `commons mcp catalog` and `commons mcp add-preset <id>` (or **Add** in the dashboard) connect major
vendors in one step. The catalog is a versioned data file (`src/commons_memory/data/mcp_catalog.json`). You can extend
it with `~/.commons/mcp_catalog.json`. Each preset declares its transport, endpoint/command, auth type, required secret
refs, default per-tool risk tags and a health check (`tools/list`).

*Verified* presets were checked on 2026-10-11 against each vendor's own docs or official repo, linked in the `docs`
field:
- **Remote endpoints:** GitHub (`api.githubcopilot.com/mcp/`, plus a local Docker image), GitLab (`/api/v4/mcp`),
  Atlassian Rovo (`mcp.atlassian.com/v2/mcp`), Linear, Notion, Slack, Google Gmail / Drive / Calendar (Workspace MCP
  endpoints), Cloudflare API + docs, Stripe, Supabase, Vercel, Sentry, PagerDuty, Datadog, HubSpot, Salesforce
  hosted MCP, Figma.
- **Local servers:** Azure MCP (`@azure/mcp`), AWS API MCP (`awslabs.aws-api-mcp-server`), Shopify Dev MCP, Playwright
  MCP, filesystem reference server.

Zapier is listed as *community/unverified* because its URL is user-specific. Microsoft 365 is omitted because no
official general-purpose server was verified. OAuth-only servers need you to obtain a token once and store it as a
secret ref; Commons does not run interactive OAuth flows yet.

**Dashboard redesign.** The dashboard is dark-first with a light-theme toggle and a CSS-variable design system:
- glassy panels, gradient accents and a sidebar with icons
- a **⌘K / Ctrl-K command palette** for pages, librarian locations and memory
- keyboard shortcuts: `1–9`, `[ ]`, `t`, `r`, `+/-`, `?`
- a live activity feed, animated per-agent status dots, SVG sparklines and stacked daily cost bars
- toasts, empty states, focus rings, `prefers-reduced-motion` support and a mobile layout

It needs no build step and has no dependencies. ![mobile](docs/img/dashboard-mobile.png)

![Discover: trending and popular public skills](docs/img/dashboard-discover.png)
![Import local skills](docs/img/dashboard-import.png)

### Shared context store (handoffs without re-paying tokens)

Agents snapshot their working context into a **context pack**: L0 gist (one paragraph), L1 brief (goal / state /
decisions / open threads / next steps), L2 detailed titled sections (notes, file excerpts), L3 raw refs (ids, paths +
hashes, URLs; never inlined). Sections are content-addressed (sha256) blobs, so identical text is stored once across
packs and agents, and `delta=true` snapshots send only changed sections. A handoff or `submit_task(context_pack=...)`
passes the **pack id**; each subtask receives only the L1 within a token budget and can `context_load(pack, "L2",
sections=[...])` or L3 refs if it needs them.

Prompt caching: the L0+L1 block is rendered as a byte-stable, timestamp-free **prefix** and placed first in worker
prompts. `ProviderLayer.chat(..., cache_prefix=...)` sends it as an Anthropic `cache_control: ephemeral` system block,
or as the leading message for OpenAI/OpenRouter/xAI (they cache identical prefixes automatically). Cached input tokens
(`cache_read_input_tokens` / `prompt_tokens_details.cached_tokens`) go into `usage.cached_tokens` and cost estimates
at about 10% of the input price. Token counts are estimated per model: tiktoken for OpenAI models when installed,
otherwise a calibrated chars-per-token ratio. `context_fit(pack, model)` picks the deepest layer that fits the receiving
model's context window (from the model registry, or known defaults).

Tools: `context_save / context_load / context_diff / context_fit`. CLI: `commons context list|load|diff|fit|savings`.
The dashboard **Context** page shows packs, per-layer token sizes, new vs reused bytes, delta diffs, tokens saved by
layering, and provider cache hits.

![Context packs](docs/img/dashboard-context.png)

## v0.3.0 —: everything is a graph, and every project has a living knowledge base

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


## v0.5.3
- `remember` accepts `text=`, and `record_decision` accepts `decision=`/`rationale=`. Clear errors replace raw validation 400s. (Found by Muse.)

## v0.5.2
- Clients that authenticate only with the `Authorization: Bearer` header now refresh `last_seen`, so presence and escalation stop treating them as offline.

## v0.5.1
- Remote `commons worker --url` now sends the Bearer header.
- `serve --dashboard` follows `--host` (new `--dashboard-host` option).
- `secret-exec` masks the secret in child output (`--no-mask` to opt out).
- Fixed an Org chart crash.
- Reading an unknown KB bucket no longer creates it.
- Clean CLI errors instead of tracebacks.
- Skill and ACL error messages now say what to do next.
- ACL bootstrap: the sole author of a project with no ACL can make it private.

## v0.2.0 / v0.1.0
recurring tasks that carry unfinished work forward · user preference learning injected into every task · headless
`commons worker` for CLI agents · a local web dashboard · usage/cost tracking · kill switch, backups and hardening.
See [FLAWS_AND_GAPS.md](FLAWS_AND_GAPS.md) for known risks.

v0.1 features remain: shared memory with hybrid search and token-budgeted results, one graph across memory/code/sessions,
code indexing, handoffs, reasoning log, agent inbox, secret references, conflict detection.
