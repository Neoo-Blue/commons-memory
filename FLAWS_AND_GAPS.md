# Flaws and gaps (v0.9)

This is the list of risks and gaps we know about. v0.5.0 worked through every open item from the v0.4 list and the
stubbed or unfinished items from the v0.1–v0.4 reports. v0.6.0 adds retro learning and remote access, and their risks
are listed here too. Part 1 covers what is fixed and how it was tested. Part 2 covers what is still open and why. If
you run Commons anywhere other than one trusted machine, read Part 2 first.

Tests: `pytest` gives 109 passed and 1 skipped on SQLite. The skipped one is the Postgres-only test. With
`COMMONS_TEST_PG=postgresql://…`, all 110 tests pass against PostgreSQL 16 + pgvector.

## Part 1: Fixed in v0.5

| Area | Was open | What v0.5 does | Tested by |
|---|---|---|---|
| **Scale / backend** | Vector search was brute force in Python. Postgres/pgvector was only "planned" (a DDL stub). | A full PostgreSQL backend (`COMMONS_DB=postgresql://…`, `pip install commons-memory[postgres]`). The whole store runs on it through a dialect layer (`pgcompat.py`): placeholders, upserts, `IS`, `LIKE`→`ILIKE`, `PRAGMA table_info`, AUTOINCREMENT→BIGSERIAL. FTS5 becomes a `tsvector` column with a GIN index. Embeddings get a `vector(n)` column queried with `<=>` (HNSW index if the extension is present). Isolated schemas via `?schema=`. `commons migrate-postgres DSN` copies an existing SQLite DB. | The entire suite runs against a local Postgres 16 + pgvector (`COMMONS_TEST_PG`). There is also a dedicated pgvector + migration test. |
| **Encryption at rest** | None | SQLCipher: set `COMMONS_DB_KEY` (`[encryption]` extra). A wrong key raises a clear error. Backups can be AES-GCM encrypted (`COMMONS_BACKUP_KEY`, `commons backup --encrypt/--decrypt`). | The test checks that plaintext is absent from the file, that the DB reopens with the key, and that a wrong key is rejected. Also tested: encrypted backup → decrypt → open. |
| **HTTP MCP auth** | No OAuth, no rate limiting, reads were unauthenticated | In HTTP mode every request (reads included) needs an agent API key or an OAuth 2.1 bearer token. OAuth tokens are checked by RFC 7662 introspection against your IdP (`COMMONS_OAUTH_ISSUER/INTROSPECT_URL`), and the protected-resource metadata is served at `/.well-known/oauth-protected-resource`. A 401 includes `WWW-Authenticate` with `resource_metadata`. Token-bucket rate limiting is per principal/IP (`COMMONS_RATE_PER_MIN`) and returns 429. Anonymous reads are opt-in only (`COMMONS_ALLOW_ANON_READS=1`). | ASGI tests cover 401, key auth, OAuth via a mock IdP, a bad token, 429 and metadata. A live `commons serve --transport http` smoke test was also run. |
| **Per-project ACLs** | None | `commons acl grant PROJECT AGENT --role read/write/admin` (also MCP and dashboard). Granting any entry makes a project private. Search, expand, KB, context packs and writes enforce it for the authenticated principal. The local owner (stdio/CLI) keeps full access. | Test: a non-member can't search, expand or write. A member can. |
| **Memory poisoning / trust** | No per-agent trust scores | `trust.py` computes per-agent trust from authenticated writes, subtask success rate, gate verdicts, conflicts lost and redaction hits. Admins can pin a score. Trust weights search ranking (low-trust hits are labelled), and KB digests drop authors below `COMMONS_KB_MIN_TRUST`. There is a trust table in the dashboard, the CLI and MCP. | Test: a trusted agent outranks an untrusted one, and the low-trust item is excluded from the KB. |
| **KB amplifying poisoned memories** | The KB did not weigh sources | Same trust weighting plus the KB floor | Same test |
| **Deadlocks: unassignable tasks** | A task type with no profiled agent would wait forever | An escalation ladder (`escalation.py`, background job, `commons escalate`, MCP `escalate_stuck`). A stuck or unassignable subtask (1) is reassigned to the most recently active agent, with an inbox message. (2) Failing that, the distributor/planner model runs it through the provider layer. (3) Failing that, the user gets a notification (dashboard, `commons notifications`, and an optional webhook `COMMONS_NOTIFY_WEBHOOK`). Each step happens once per subtask. Nothing waits silently. | Test: all three steps, plus no repeat notification. |
| **Missed schedules** | Missed runs weren't back-filled | A per-schedule catch-up policy: `skip`, `once` (default) or `all` (capped by `max_catchup`). Missed counts are recorded. | A parametrised test runs all three policies after 5 h of downtime. |
| **KB staleness / background** | No background thread without `commons scheduler` | `background.py`: an in-process daemon thread in `commons serve`/`dashboard` rebuilds dirty KB pages after the debounce. It also runs the incremental librarian, recurring runs, lease reaping, escalation, routing validation, retention and backups, each on its own interval (`COMMONS_EVERY_*`). A leader lease in `meta` keeps several processes on one DB from double-running. Status shows on the dashboard System tab and through MCP `maintenance`. | Test: KB rebuilt with no scheduler. Thread stops cleanly. |
| **Graph growth / usage records** | No pruning of usage | `commons prune` (and a background job) rolls usage rows older than `COMMONS_RETAIN_USAGE_DAYS` into `usage_daily`. Summaries stay exact. Old audit/MCP-call logs, token-count caches and stale graph links are trimmed too. | Test: totals are identical before and after the rollup. |
| **Context blob growth** | No GC of unreferenced blobs | Old context packs (`COMMONS_RETAIN_CONTEXT_DAYS`; never the latest per name or packs used by running jobs) are deleted, and blobs no pack references are GC'd. | Same test |
| **Data loss / lock-in** | No scheduled backups, no import tool | Scheduled rotated backups (`COMMONS_BACKUP=1`, keep N, optional encryption). `commons dump` / `commons import-memory` do a full-fidelity, idempotent, cross-dialect JSONL round trip. Import also accepts the old `export-memory` format. | Dump → load into a fresh store, and a second load adds no duplicates |
| **Topic clustering** | Keyword-only | Semantic clustering (greedy centroid on embeddings, `COMMONS_KB_CLUSTER=semantic|auto`), named by top terms. Falls back to keyword clustering. | Test: two unrelated themes separate into two clusters |
| **Token estimates** | len/4 except tiktoken for OpenAI | `count_tokens(..., exact=True)`: tiktoken for OpenAI models. The Anthropic `messages/count_tokens` and Gemini `countTokens` APIs for those providers. Results are cached by hash. Calibrated ratios remain the fallback. | tiktoken exact match, and the provider path via the mock provider plus the cache |
| **Worker presets** | Unverified CLI flags | `claude -p --output-format text --permission-mode acceptEdits`, `codex exec --full-auto --skip-git-repo-check`, `cursor-agent -p --output-format text --force`, `gemini -p … --yolo` (prompt via stdin), plus `cursor-legacy`. Each is checked against the vendor's current docs, with notes in `docs/workers.md`. | Doc review, see Part 2 |
| **Secret leakage** | Regex only, no entropy detection | Shannon-entropy detection for novel token formats. It ignores hashes, UUIDs, Commons ids and paths. More vendor patterns were added (HF, GitLab, npm, SendGrid, DigitalOcean, xAI). | Test: a novel token is caught, and SHAs/paths/ids are left alone |
| **Infisical adapter** | Stub | A real adapter: universal-auth login (`INFISICAL_CLIENT_ID/SECRET`) or a service token. `GET /api/v4/secrets/{name}` with project/env/path, token caching. Refs look like `infisical://PROJECT/ENV/path/NAME`. | A mock server implementing the documented endpoints |
| **Vault dynamic secrets** | KV only | `vault-dyn://mount/creds/role#field` issues a fresh credential per lease. Revoking the Commons lease (or expiry plus the reaper) revokes the Vault lease (`PUT sys/leases/revoke`). Values live only in process memory. AppRole login is supported. | Mock Vault: distinct creds per lease, plus revoke on revoke and on expiry |
| **A2A** | Client only, no server, no streaming/polling | An A2A server at `/a2a` with the agent card at `/.well-known/agent-card.json` (JSON-RPC: `message/send`, `message/stream` (SSE), `tasks/get`, `tasks/cancel`, `tasks/resubscribe`). Tasks map onto Commons jobs. Client side: `send_message`, `get_task`, `wait_task` (polling), `cancel_task`, `stream_message`, plus the `commons a2a` CLI and the MCP `a2a_task`. | Test: send/get/cancel/error, and streaming with live workers until `completed` and an artifact |
| **Code graph** | JS/TS regex-only, call targets resolved by name | Optional tree-sitter (`[code]` extra) for JS/TS/TSX: functions, classes, methods, arrow functions, imports (default, named, aliased, namespace), `this.` calls. Fallback to regex is kept. Call targets in both Python and JS are resolved by scope: local, then imported module/alias, then self/this, then a unique global name. Each edge records how it was resolved. | Two-module same-name fixtures for Python and TS, with tree-sitter on and off |
| **Dashboard exposure** | No TLS, single shared token | `--tls-cert/--tls-key` or `--tls-self-signed` for both the dashboard and `serve`. Per-user tokens (`commons dashboard-user add NAME --role viewer|admin`): viewers are read-only. | HTTPS test with a self-signed cert. A viewer POST returns 403. |
| **Dashboard live updates** | Polling | Server-Sent Events (`/events`) push change notifications, and the page refreshes the active tab. Falls back to polling. A new System tab shows background jobs, notifications, escalations, trust, ACLs and routing validation. | SSE test: a change event arrives after a write |
| **Distributor mistakes** | No A/B validation after a switch | Every routing switch opens a validation window. Post-switch scores (real task grades and probes) are compared with the old model's baseline. A regression either proposes or auto-applies a revert. | Test: a regression produces a revert proposal |
| **MCP hub** | Reconnect per call. Credentials shared by all agents. No OAuth. | Persistent pooled sessions per (server, credential set), idle-closed, auto-reconnect. Per-agent credential overrides (`commons mcp agent-creds`, with an optional "require own creds"). OAuth 2.0 client-credentials / refresh-token grants for upstreams (`commons mcp oauth`), with token caching. All of these are still secret refs. | Test: session reuse, a separate session per agent identity, the require rule, and rejection of raw secrets |
| **Catalog drift** | Needed manual re-verification | `commons mcp verify-catalog` probes the docs URL, the remote endpoint and the npm/PyPI package for each preset and stores the result | Manual run (needs network) |
| **Duplicate work** | Semantic duplicates weren't detected | Job submissions (`dedupe=True`, the default over MCP) and leftovers are checked by embedding similarity (`COMMONS_DUP_SIM`) against open work | Test: a reworded job and leftover are detected |
| **Research poisoning** | Not fact-checked | Corroboration: each claim is checked for support from independent domains. Single-source findings are tagged `single-source` and flagged in summaries. | Test |
| **Local import side effects** | Restore was manual | `commons skills backups` / `commons skills restore ID` | Manual check |
| **Secret refs in graph exports** | Reference names leaked | Exports replace secret-ref names with stable hashes (opt out with `COMMONS_EXPORT_SECRET_REFS=1`) | Test |
| **Observability** | No tracing | Optional OpenTelemetry (`[otel]`, `COMMONS_OTEL=1|console`, OTLP if `OTEL_EXPORTER_OTLP_ENDPOINT` is set) adds spans for MCP tool calls, LLM calls, hub calls, claims and background jobs. Spans carry ids and sizes only. | Test: a span is recorded for an LLM call |
| **Planner / summariser / preferences** | Hook-only | Built in through the provider layer roles (`planner`, `kb_summarizer`, `pref_extractor`, `librarian`), finished in v0.4. The env hooks still work. | Existing tests |

## New in v0.6: retro learning and remote access

| Area | What v0.6 does | Tested by |
|---|---|---|
| **Retro learning** | `commons learn`, the MCP `retro_learn` / `retro_queue` / `retro_decide` tools and a dashboard page. It parses Claude Code, Codex, Cursor (state DBs), ChatGPT and Claude.ai exports, Gemini CLI, generic transcripts, git and PRs (via gh), and memory files (CLAUDE.md, AGENTS.md, .cursor/rules, mem0, basic-memory). Then it redacts, chunks and extracts with an LLM under a budget or with heuristics, dedupes, and sends everything to a review queue. Checkpoints keep re-runs incremental. Dry runs estimate tokens and cost. | `tests/test_retro.py`: a fixture history for every format, dry run, queue/apply/idempotency/append, auto-approve floor, LLM path plus budget fallback, dashboard API |
| **Remote access** | `commons expose` covers Tailscale serve/funnel/bind, Cloudflare quick and named tunnels plus Access JWT validation, ngrok CLI/SDK with OAuth/CIDR passthrough, SSH tunnels (localhost.run), bore, ZeroTier/WireGuard bind, and Caddy/nginx configs. Health and URL appear in `commons status` and the dashboard. Public exposure is refused unless a live probe gets 401. Client snippets are regenerated with the reachable URL. There is also a Dockerfile, docker-compose, and systemd/launchd/Windows units. | `tests/test_expose.py`: mock CLIs for every provider, the auth guard, Access JWT (RS256 + JWKS), snippets, units, status |

## New in v0.7: messaging, wake, routing, Drive, multitasking

| Area | What v0.7 does | Tested by |
|---|---|---|
| **External bus dependency** | Native messaging plus a `/bus/*` shim, SSE/WS streams, wake channels (signed webhooks with backoff and dead-letter, ntfy, Pushover, Slack, Discord, Telegram, email, command), presence and wake-on-demand, `agent daemon`, `bus import` | `tests/test_v07_messaging.py`: mock webhook receiver, replay guard, dead-letter/redrive, SSRF, bus shim + spoofing, WS, SSE, bus import, daemon |
| **A2A push notifications (deferred in v0.5)** | Enabled behind the outbound URL allowlist | `tests/test_v05.py` A2A tests |
| **Agent-only routing** | Agent + model + effort targets, a policy table, difficulty estimation, escalation and de-escalation, provider and CLI effort mapping, recorded rationale, `route_why` | `test_routing_records_target_effort_and_escalates` |
| **No shared file store** | Commons Drive with versions, locks, optimistic concurrency, secret scanning, indexing, quotas, ACLs, artifact links, host mounts, git/rclone sync | `tests/test_v07_drive_sched.py`: local, mock S3, mock WebDAV, fake FTP, host mounts, concurrent writers, ACL, git |
| **Single-job-at-a-time feel** | Fair-share multi-project scheduler with priorities, limits, budgets, agent capacity, preemption and cross-project dependencies, plus Mission control | Stress test: 5 projects × 3 jobs, 6 concurrent agents, no duplicate claims |
| **Inconsistent tool arguments** (Muse critique) | Shared alias layer + friendly errors on every tool; project-name validation (rejects names like `commons-live?`) | `test_friendly_tool_args_and_project_names` |

## New in v0.7.2: Docker image and tokenless private dashboard

| Area | What changed | Tested by |
|---|---|---|
| **Container** | Multi-stage non-root image with tini, a healthcheck, the `/data` volume, docker-secrets `*_FILE` loading and a Postgres wait. Compose profiles cover postgres/pgvector, tailscale, cloudflared and ngrok. Multi-arch amd64/arm64 | Built and run with rootless Podman: 401 without a key, agent registration, MCP tool call, dashboard 200, persistence across container recreation, the compose postgres profile. `tests/test_v072_docker.py` |
| **Fresh-DB race** | Two threads registering the same built-in agent (`librarian`) at first start crashed the server | Test: 8 concurrent `ensure_agent` calls |
| **Dashboard on private networks** | No token needed on loopback/LAN/tailnet binds. Wildcard and public binds still need one. `COMMONS_DASHBOARD_REQUIRE_TOKEN=1` forces it | Tests |
| **Private-network trust** | Anyone who can reach a private-address dashboard can use it (admin actions included) when no token is set | That is the requested trade-off. Set `COMMONS_DASHBOARD_REQUIRE_TOKEN=1` on shared LANs or tailnets. Agent keys still protect MCP. |

## New in v0.8.0: retrieval quality and capture
- Fixed: the default embedder was a lexical hash. It is now fastembed when installed, or an OpenAI-compatible
  endpoint. Hash is kept as the fallback.
- Fixed: ranking ignored entities and time. It now has entity, time-proximity and recency signals, and
  `explain` shows their contribution.
- Fixed: there were no published benchmarks. Retrieval benchmarks are now reproducible, with per-question results.
- Fixed: agents had to call memory tools manually. Lifecycle hooks and the observation pipeline now capture
  automatically.
- Open: end-to-end QA accuracy (answering with a reader LLM) isn't benchmarked yet. It needs an LLM budget.
- Open: on LoCoMo, the recency signal lowers R@1. Default weights are untuned.
- Verified only against the documentation: the Codex, Gemini and Cursor hook formats. The installers write the
  documented schemas, but those CLIs are not installed on the build box. Claude Code's format matches its
  documented schema.

## New in v0.9.0: found and fixed by two independent defect reviews and live use

| Area | Was | Now | Tested by |
|---|---|---|---|
| Tool overload | 148 tool schemas (33k tokens) in every client session | Core profile of 21 tools (2,970 tokens) plus `tools`/`call`; profiles per URL, agent or env | `test_core_tool_profile` |
| Hub freezes | Sync tools ran on the event loop: one slow call or a 25 s long-poll blocked every agent (`whoami` 130 s) | Sync tools run in worker threads | `test_slow_sync_tool_does_not_block_others`, live NAS measurement (0.3 s) |
| Search at scale | Pure-python cosine over every vector (30k items: 1.4 s per search) | Cached numpy matrix + FTS rank-then-filter (68 ms) | `commons bench scale`, LoCoMo unchanged |
| Ranking | RRF flattened scores, so recency/entity boosts overrode relevance; `as_of` leaked future items | Score fusion; `as_of` cutoff; true totals | LoCoMo/LongMemEval, `test_update_delete_keep_indexes_in_sync` |
| ACL bypasses | KB, memory_stream, facts, graph reads, expand on non-item ids, timeline_around, get_task, why ignored project ACLs | One batched `hidden_ids` filter on every read path | `test_every_read_path_honours_project_acls` |
| Host filesystem | Any authenticated agent could `export_graph(path=...)` anywhere or `index_code` any directory | Local owner or `COMMONS_ADMIN_AGENTS` only; host mounts can't escape via symlinks | `test_remote_agents_cannot_touch_host_filesystem` |
| DNS rebinding | Tokenless private dashboards answered any Host header | Only IPs, localhost, this machine's names, `*.ts.net` and `COMMONS_DASHBOARD_HOSTS` | `test_tokenless_dashboard_refuses_rebinding_hosts` |
| Races | Heartbeat/reaper/complete could overwrite each other; two agents could both "win" an inbox task or a message | Conditional updates with rowcount checks | `test_inbox_claim_race_has_one_winner`, `test_concurrent_recv_...` |
| SSRF / hijack | Email wake channels reached any host; anyone with an A2A task id could cancel it or redirect its push | SSRF check on SMTP hosts; owner-only A2A task methods | same test |
| Wake | Assignments never reached `commons agent daemon` (it read only chat messages), and `--cmd` disabled the command | Mailbox delivery, `summon`, working daemon with presets and `--install` | `test_instant_wake_...` |
| Re-index | Re-indexing deleted memory->code links and was not atomic | Non-code edges re-attached; wipe inside the transaction | `test_reindex_keeps_memory_links_and_is_atomic` |
| Stale keyword index | Tag edits (librarian, research) left FTS rows stale | `sync_fts` on every edit, `update_item`/`delete_item` | same test |
| Cross-process complete | Two workers on separate connections could both pass the ownership check and both write usage and budget | Conditional status UPDATE decides the winner before any side effect | `test_concurrent_complete_separate_connections_keep_job_state` |
| NAS CPU pinned | An import's reindex and the background reindex embedded the same rows at once; a bulk import's `defer_embed` also stopped every other agent's memories from being embedded | One reindex at a time (others return busy); `defer_embed` per thread; the librarian's 165k-row "where to find" index rebuilds hourly instead of every 10-minute pass | py-spy on the live hub, import 6x faster |
| Lock held across ONNX | `complete_subtask` embedded its result while holding the store lock, stalling every agent | Embedded after the lock | concurrency suite |
| Handoff session | `handoff` did not return its `session_id`, so the next agent could not `resume()` it precisely | Returned | live multi-agent run |
| Postgres | Scalar `MAX(a,b)` (scheduler preemption) failed | Translated to `GREATEST`; full suite passes on Postgres 17 + pgvector | `COMMONS_TEST_PG` run |
| Free-text contradictions | Only structured facts (subject/predicate) were checked | Librarian asks TypeSafe about each new note vs its 3 nearest (precision 1.00 / recall 0.97 on 96 pairs) | `test_librarian_flags_free_text_contradictions_once` |

### Still open after v0.9
| Risk / gap | Why |
|---|---|
| QA vs mem0 | Run head-to-head here (same reader/judge/prompts): mem0 70.3%, Commons 69.5%, a statistical tie. mem0's LLM-extracted facts win multi-hop (61 vs 53) and temporal (74 vs 69); Commons pays nothing per write and searches 6-15x faster. LongMemEval QA: 62% on a 100-question sample. |
| Multi-hop and temporal QA | One retrieval pass over raw turns misses facts spread across turns; mem0 consolidates them at write time with an LLM. Query decomposition or optional write-time distillation is not built. |
| Postgres speed | As wired (one connection, translated SQLite dialect, many small queries) it is 4-10x slower than SQLite in `bench scale`; use it to share a database, not for speed. |
| Per-thread SQLite readers | `COMMONS_SQLITE_READERS=1` exists but measured slower on an oversubscribed Mac; off until measured on the target host. |
| Archify parity | Graphs are real and analysed, but there are no sequence, workflow or lifecycle diagram types, no commit-pinned source badges and no architecture diff between snapshots yet. |
| Instant wake for chat-only AIs | A browser chat can't run while idle. Commons wakes daemon-backed agents instantly; chat agents see work on their next `msg_recv`. |
| Vector cache | SQLite only (Postgres uses pgvector). The matrix lives in each process's memory (60k x 384 floats = 92 MB). |
| Curated import | Costs model tokens (about $1 per 1,000 claude-mem items with Haiku 5.5) and cannot know which facts were fixed later; review the queue. |
| Unlisted tools | Some clients only call listed tools; they reach the others through `call`. |
| Postgres tsquery with operator-like words | Reported by review; the full suite now passes on Postgres 17, but this case has no dedicated test. |
| `add_item` is not one transaction | A crash between statements can leave a memory without its graph links. |

## Part 2: Still open, and why

| Risk / gap | Status in v0.5 | Why it is still open |
|---|---|---|
| **Prompt injection between agents** | Fencing of upstream outputs, redaction, untrusted wrappers, trust weighting, and secrets that never reach models | No known general fix exists. A model can always be persuaded by data. Run workers sandboxed. |
| **Worker presets verified by running** | Flags checked against the vendors' current docs (see `docs/workers.md` for sources and dates) | The build machine has no logged-in Claude Code, Codex, Cursor or Gemini CLI accounts, so the presets were not run end-to-end. CLI flags change often. Use `--cmd` to override. |
| **Live Vault / Infisical / OAuth IdP / provider token-count APIs** | Implemented against the documented HTTP APIs and tested against faithful mock servers | Testing them live needs real accounts and credentials (Vault server, Infisical project, IdP, Anthropic/Google keys) that the build environment does not have. |
| **OAuth authorization server** | Commons is an OAuth 2.1 resource server (introspection + protected-resource metadata) | Commons does not issue tokens or run an interactive login. That needs an external IdP (Auth0, Keycloak, Entra…). Local JWT/JWKS verification is not built in. Introspection is used instead. |
| **Interactive OAuth for upstream MCP servers** | Client-credentials and refresh-token grants | Browser-based authorization-code flows need a human and a redirect endpoint. Obtain a refresh token once and store it as a ref. |
| **Rate limiting across replicas** | Per-process token buckets | A shared limiter needs Redis or similar. With several replicas, rate-limit at the proxy. |
| **Maintenance leader election** | A lease row in `meta` (read-then-write) | Not a strict distributed lock. Two processes starting at the same instant could both run one tick. All jobs are idempotent, so this is harmless, but for strictness set `COMMONS_BACKGROUND=0` on all but one process. |
| **Postgres FTS ranking** | English `tsvector` + `ts_rank`. pgvector is used when the extension exists, otherwise brute force | Ranking differs slightly from SQLite FTS5/BM25. Other languages need a different text-search config. |
| **SQLCipher availability** | Optional extra (`sqlcipher3`) | It needs a SQLCipher build. Wheels exist for Linux. Elsewhere, install SQLCipher or use disk encryption. Postgres encryption at rest belongs to the database/disk layer. |
| **Trust gaming / collusion** | Trust comes from observed behaviour, with admin pins | One person running every agent on the same model is still one opinion. Trust is heuristic. A patient attacker can build up trust before abusing it. |
| **Cost accuracy** | Exact token counts where an API or library exists. Provider-reported usage is preferred | Prices come from provider APIs when they publish them. Otherwise they come from a table that must be kept current. Self-reported usage from external agents can't be verified. |
| **Semantic clustering quality** | Works with any embedder | With the default hash embedder, "semantic" is still lexical. Install `[embed]` (fastembed) for real semantic clusters. |
| **Drive backends without a live server** | S3 (mock), WebDAV (mock), FTP (fake ftplib) are tested. SFTP (paramiko) and SMB (smbprotocol) are implemented but not run | No SFTP server, SMB share or cloud bucket in the build environment. Google Drive/OneDrive/Dropbox go through rclone, which is not verified without accounts. |
| **Wake for humans (email/Pushover/Telegram/Slack/Discord)** | Implemented against documented APIs. Webhook paths tested with mocks | No accounts in the build environment. Command hooks run on the Commons host, so only the local owner can add them. |
| **Existing invalid project names** | New names are validated | Projects created before v0.7 with odd names (e.g. `commons-live?`) are not renamed automatically. Fix them with `commons project-rename 'commons-live?' commons-live`. |
| **Multi-process wake dispatch** | Deliveries are claimed atomically, so two processes never send the same delivery twice | The live SSE/WS hub is per process. Behind several replicas, a stream only sees wakes emitted by its own process (webhooks and other channels are unaffected). |
| **Pattern-based scanners** (skills, redaction, risk tags) | Broader patterns plus entropy detection | Heuristic by nature. Novel formats or obfuscation can slip through, and very random non-secrets can be redacted. |
| **Skill scripts not sandboxed** | Review gates, scanning, pinning, license checks | Sandboxing needs OS-level isolation (containers, seccomp), which is outside a Python library's reach. |
| **Research truth** | Corroboration across independent domains | Agreement isn't truth. Several sites can repeat the same false claim. |
| **Popularity ≠ safety** in skill discovery | Stars, deltas and review | Signals from GitHub can be gamed. |
| **A2A push notifications** | Streaming and polling are implemented. The card advertises `pushNotifications: false` | Webhook push would need outbound callbacks to client-supplied URLs (an SSRF risk). Deferred until there's an allow-list design. |
| **Dashboard user management** | Per-user tokens with roles | No SSO/OIDC login for the dashboard. Put it behind your IdP's proxy if you need that. |
| **Retro learning: format drift** | Parsers follow the formats as of 2026-10 and skip anything they can't read | Claude Code, Codex, Cursor and Gemini change their history formats without notice, and none of these formats is a documented public API. Cursor's state DB is undocumented, so the parser covers the known `composerData`/`bubbleId`/`aichat` keys. Fixtures mirror real files but were not taken from live accounts. This box has no Claude, Codex, Cursor or Gemini history. |
| **Retro learning: heuristic quality** | Review-first queue with confidence and provenance on every item | Without a model, extraction is pattern-based. It misses implicit knowledge and can pick up sentences out of context. Configure a `retro` model for better recall. Performance scores from feedback phrases are weak signals. |
| **Retro learning: privacy** | Redaction runs before anything leaves the parser. The MCP tool is admin-only and dry-run by default | History often contains personal data that isn't a secret. Review the queue before approving, and use `--source`/`--path` to limit what is read. |
| **Remote access: real tunnels** | All providers are implemented against the vendors' documented CLIs and tested with mock CLIs. Tailscale serve/funnel flags were checked against the installed tailscale 1.104 help | Starting Cloudflare, ngrok or localhost.run tunnels for real needs accounts or internet-facing side effects that the build box shouldn't create. Funnel needs to be enabled in the tailnet ACLs. |
| **Docker image** | Dockerfile, compose file and publishing instructions | The build box has no Docker daemon, so the image hasn't been built or pushed in CI. Build it locally with `docker build .` |
| **ngrok via the Python SDK** | Works while the `commons expose` process stays alive | The SDK tunnel lives inside that process. Use the ngrok CLI for a detached tunnel. |
