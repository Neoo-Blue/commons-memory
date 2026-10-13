# Configuration reference

Generated from the source by `scripts/gen_env_reference.py` — do not edit by hand.
All settings are environment variables; CLI flags override them where both exist.

| Variable | Default | What it does | Read in |
|---|---|---|---|
| `COMMONS_A2A` | `1` | Mount the A2A (agent-to-agent) server next to `/mcp` on the HTTP transport (`0` disables) | server.py |
| `COMMONS_A2A_TOKEN` | — | Bearer token used by `commons a2a` client commands | cli.py |
| `COMMONS_ADMIN_AGENTS` | — | Comma list of agent names with global admin rights (ACLs, trust overrides) | auth.py, server_v5.py |
| `COMMONS_AGENT` | `cli` | Agent identity for stdio/CLI use when no API key is given | capture.py, cli.py, cli_v7.py, server.py |
| `COMMONS_AGENT_DEFAULT_CAPACITY` | `4` | Concurrent subtasks per agent when its profile sets no `max_concurrent` | orchestrator.py |
| `COMMONS_ALLOW_ANON_READS` | — | HTTP: allow read-only calls without a key (`1`); writes always need a key | auth.py, expose.py |
| `COMMONS_API_KEY` | — | Agent API key for stdio/CLI/worker calls | capture.py, cli.py, cli_v7.py, cli_v8.py, server.py |
| `COMMONS_BACKGROUND` | `light` | In-process maintenance: `light` (KB refresh, librarian, reaper), `full` (every job), `0` off | background.py |
| `COMMONS_BACKUP` | `0` | Nightly SQLite backup from the background loop (`1` on) | background.py |
| `COMMONS_BACKUP_DIR` | — | Backup directory (default `~/.commons/backups`) | retention.py |
| `COMMONS_BACKUP_KEEP` | `7` | Number of backups to keep | retention.py |
| `COMMONS_BACKUP_KEY` | — | Secret ref used to encrypt backups | retention.py |
| `COMMONS_BG_POLL` | `2` | Background loop tick in seconds | background.py |
| `COMMONS_CALL_TIMEOUT` | `300` | Seconds a remote CLI/worker call to the hub may take before it fails and is retried by the caller (default 300) | worker.py |
| `COMMONS_CAPTURE` | `1` | Global switch for hook auto-capture (0 disables everywhere) | capture.py |
| `COMMONS_CF_ACCESS_AS_AUTH` | — | Treat a valid Cloudflare Access identity as an agent principal (`1`) | auth.py |
| `COMMONS_CF_ACCESS_AUD` | — | Cloudflare Access application audience tag to verify | expose.py |
| `COMMONS_CF_ACCESS_CERTS` | — | Cloudflare Access certs URL override | expose.py |
| `COMMONS_CF_ACCESS_TEAM` | — | Cloudflare Access team name (`<team>.cloudflareaccess.com`) | expose.py |
| `COMMONS_CONTRADICTION_MAX` | `50` | New notes checked for contradictions per librarian pass (default 50) | librarian.py |
| `COMMONS_CONTRADICTION_THRESHOLD` | `0.85` | TypeSafe probability at which two notes count as contradicting (default 0.85; precision first) | librarian.py |
| `COMMONS_DAILY_COST_CAP` | — | USD/day cap across all orchestrated work; jobs pause when hit | usage.py |
| `COMMONS_DASHBOARD_HOSTS` | — | Extra hostnames a tokenless dashboard answers to (comma list). IPs, localhost, this machine's name, *.ts.net and the COMMONS_PUBLIC_URL host are always accepted; other names are refused to block DNS rebinding | dashboard.py |
| `COMMONS_DASHBOARD_REQUIRE_TOKEN` | — | Require the dashboard token even on localhost/LAN/tailnet binds (`1`) | dashboard.py |
| `COMMONS_DASHBOARD_TOKEN` | — | Admin token for the web dashboard (required for non-loopback binds) | dashboard.py |
| `COMMONS_DB` | — | Database path (SQLite) or `postgresql://…` DSN (default `~/.commons/commons.db`) | cli.py, store.py |
| `COMMONS_DB_KEY` | — | Secret ref for SQLCipher database encryption | dashboard.py, store.py |
| `COMMONS_DRIVE_BACKEND` | `local` | Commons Drive storage: `local`, `s3`, `webdav`, `hostfs`, `sftp`, `smb`, `ftp` | drive.py |
| `COMMONS_DRIVE_DIR` | — | Local Drive directory (default `~/.commons/drive`) | drive.py |
| `COMMONS_DRIVE_MAX_FILE_MB` | `25` | Largest single Drive file in MB | drive.py |
| `COMMONS_DRIVE_QUOTA_MB` | `1024` | Total Drive quota in MB | drive.py |
| `COMMONS_DUP_SIM` | `0.86` | Similarity above which a new leftover is merged into an existing one | orchestrator.py |
| `COMMONS_EMBEDDER` | `auto` | Embeddings: `auto` (fastembed if installed, else openai if COMMONS_EMBED_URL, else hash), `fastembed[:model]`, `openai[:model]`, `hash`, `none` | embed.py |
| `COMMONS_EMBED_KEY_REF` | — | Secret reference for the embeddings endpoint key | embed.py |
| `COMMONS_EMBED_MODEL` | `text-embedding-3-small` | Embedding model (fastembed default BAAI/bge-small-en-v1.5; openai default text-embedding-3-small) | embed.py |
| `COMMONS_EMBED_THREADS` | — | ONNX threads for the fastembed embedder (default: all cores) | embed.py, rerank.py |
| `COMMONS_EMBED_URL` | — | OpenAI-compatible embeddings base URL (OpenAI, Ollama, LM Studio, vLLM) | embed.py |
| `COMMONS_ENTROPY_MIN` | `4.3` | Shannon-entropy threshold for redacting unknown secret-looking strings | redact.py |
| `COMMONS_ENTROPY_REDACT` | `1` | Redact high-entropy strings on write (`0` off) | redact.py |
| `COMMONS_ENV_SECRETS` | — | Allow-list of env var names that `env://` secret refs may resolve | secrets.py |
| `COMMONS_ESCALATE_AFTER` | `900` | Seconds a ready subtask may sit unclaimed before the escalation ladder acts | escalation.py |
| `COMMONS_EVERY_*` | — | Override a background job interval in seconds, e.g. `COMMONS_EVERY_LIBRARIAN=3600` |  |
| `COMMONS_EXACT_TOKENS` | — | Use provider count-tokens APIs for exact context-pack sizes (network calls) | contextpacks.py |
| `COMMONS_EXPORT_SECRET_REFS` | — | Include secret references (never values) in graph exports (`1`) | graphexport.py |
| `COMMONS_GITHUB_API` | `https://api.github.com` | GitHub API base for skill discovery | skilldiscovery.py |
| `COMMONS_HOME` | `computed` | Commons home for `commons expose` state (default `~/.commons`) | capture.py, cli_v7.py, expose.py |
| `COMMONS_HOOK_CONTEXT_TOKENS` | `1200` | Token budget of the SessionStart context pack injected by hooks | capture.py |
| `COMMONS_HOOK_KEEP_WORKING` | — | Client side: 1 makes the Claude Code Stop hook keep the agent working while Commons has tasks waiting for it and quota allows | capture.py |
| `COMMONS_HOOK_KEEP_WORKING_MAX` | `5` | Max consecutive keep-working rounds per session before a Stop is allowed (default 5) | capture.py |
| `COMMONS_HOOK_RECALL_SIM` | `0.6` | Min similarity for prompt-time recall snippets injected by hooks | capture.py |
| `COMMONS_HOOK_TIMEOUT` | `2` | Seconds a hook waits for the server before queueing offline | capture.py |
| `COMMONS_HOST` | `127.0.0.1` | Default bind address for `commons serve` | cli.py |
| `COMMONS_HOST_ROOTS` | — | Colon/comma list of host directories the `hostfs` Drive backend may expose | drive.py |
| `COMMONS_KB_CLUSTER` | `auto` | Misc-topic clustering for the knowledge base: `auto`, `semantic`, `tags` | kb.py |
| `COMMONS_KB_CLUSTER_SIM` | `computed` | Cluster similarity threshold (0.55 with real embeddings, 0.35 with `hash`) | kb.py |
| `COMMONS_KB_DEBOUNCE` | `10` | Seconds between KB rebuilds of a dirty project | kb.py |
| `COMMONS_KB_MAX_STALE` | `600` | Seconds a dirty knowledge-base bucket may stay stale while writes keep arriving (default 600); otherwise it rebuilds after COMMONS_KB_DEBOUNCE seconds of quiet | kb.py |
| `COMMONS_KB_MIN_TRUST` | `0.2` | Minimum author trust for a record to be cited in the KB | kb.py |
| `COMMONS_KB_SUMMARIZER` | — | Shell command that post-processes each KB section (markdown on stdin/stdout) | kb.py |
| `COMMONS_LEASE_SECONDS` | `900` | Default subtask lease; expired leases are reassigned | orchestrator.py |
| `COMMONS_LIBRARIAN_AUTO_APPROVE` | — | Apply librarian proposals without review (`1`) | librarian.py |
| `COMMONS_LIBRARIAN_BUDGET` | `0.10` | USD budget per librarian run when it delegates to an LLM | librarian.py |
| `COMMONS_LIBRARIAN_DUP` | `0.9` | Similarity above which the librarian proposes merging records | librarian.py |
| `COMMONS_LIBRARIAN_INDEX_EVERY` | `3600` | Seconds between full rebuilds of the librarian's "where to find X" index during incremental passes (default 3600; a full pass always rebuilds) | librarian.py |
| `COMMONS_LIBRARIAN_STALE_DAYS` | `90` | Age after which the librarian flags a record as stale | librarian.py |
| `COMMONS_MCP_CACHE_TTL` | `300` | Seconds to cache read-only upstream MCP results | mcphub.py |
| `COMMONS_MCP_CATALOG` | `computed` | Path to the MCP server catalog override | mcphub.py |
| `COMMONS_MCP_HOST` | `127.0.0.1` | MCP bind host as seen by the dashboard Connect page (set by `commons serve`; override for split deployments) | cli.py, connect.py |
| `COMMONS_MCP_IDLE` | `300` | Seconds before an idle pooled upstream MCP server is stopped | mcphub.py |
| `COMMONS_MCP_PERSISTENT` | `1` | Keep upstream MCP servers running between calls (`0` = spawn per call) | mcphub.py |
| `COMMONS_MCP_PORT` | `8765` | MCP port as seen by the dashboard Connect page (set by `commons serve`) | connect.py |
| `COMMONS_NOTIFY_WEBHOOK` | — | Webhook for escalation notifications to a human | escalation.py |
| `COMMONS_OAUTH_AUDIENCE` | — | Expected `aud` of OAuth access tokens | auth.py |
| `COMMONS_OAUTH_CLIENT_ID_REF` | — | Secret ref of the OAuth introspection client id | auth.py |
| `COMMONS_OAUTH_CLIENT_SECRET_REF` | — | Secret ref of the OAuth introspection client secret | auth.py |
| `COMMONS_OAUTH_INTROSPECT_URL` | — | RFC 7662 token introspection endpoint | auth.py |
| `COMMONS_OAUTH_ISSUER` | — | OAuth issuer; enables OAuth bearer tokens alongside agent keys | auth.py |
| `COMMONS_OTEL` | `0` | Export OpenTelemetry traces/metrics (`1`, uses standard `OTEL_*` vars) | telemetry.py |
| `COMMONS_PLANNER_CMD` | — | Shell command that turns a task into a DAG (used by `pattern=llm`) | orchestrator.py |
| `COMMONS_PORT` | `8765` | Default port for `commons serve --transport http` | cli.py |
| `COMMONS_PREF_EXTRACTOR` | — | Shell command that extracts preferences from text (one per line) | prefs.py |
| `COMMONS_PREF_HALF_LIFE_DAYS` | `120` | Half-life for preference confidence decay | prefs.py |
| `COMMONS_PRICES` | — | Path to a JSON price table (USD per 1M tokens) for cost estimates | usage.py |
| `COMMONS_PUBLIC_URL` | `computed` | Externally visible base URL (OAuth metadata, A2A card, wake links) | connect.py, dashboard.py, server.py |
| `COMMONS_QA_DATES` | — | bench qa-*: `iso` prints memory dates as 2023-05-08 (the v0.9.0 style); default "8 May 2023" | bench.py |
| `COMMONS_QUOTA_BACKOFF` | `3600` | Seconds to park work when an agent hits its limit without a known reset time (default 3600) | orchestrator.py |
| `COMMONS_QUOTA_REPORTERS` | — | Agents allowed to report quota windows on behalf of other agents (comma list), e.g. the agent whose key runs `commons quota sync` from aiusage | server_v8.py |
| `COMMONS_QUOTA_RESERVE` | `20` | Default % of every quota window kept for the user (default 20); per period: COMMONS_QUOTA_RESERVE_5H, _WEEKLY, _DAILY, _MONTHLY; per agent: profile limits.reserve | quota.py |
| `COMMONS_QUOTA_TASK_PCT` | `5` | Assumed % of a window one subtask uses until Commons has learned that agent/task type (default 5) | quota.py |
| `COMMONS_RANK_FUSION` | `score` | How keyword and vector candidates are fused: `score` (default; normalised-score sum, keeps how much better a match is) or `rrf` (reciprocal-rank fusion, the v0.8 behaviour; `COMMONS_RANK_RRF_K` sets k) | ranking.py |
| `COMMONS_RANK_SIM_FLOOR` | `0.3` | Minimum cosine similarity for a vector hit (non-hash embedders) | store.py |
| `COMMONS_RATE_PER_MIN` | `600` | Per-principal HTTP request limit per minute (excess gets 429) | auth.py |
| `COMMONS_REINDEX_BATCH` | `512` | Items re-embedded per background tick after an embedder change | background.py |
| `COMMONS_REQUIRE_KEYS` | `1` | HTTP: require an agent key/OAuth token on every request (`0` = local dev only) | cli.py, expose.py, server.py |
| `COMMONS_RERANK` | `off` | Second-stage rerank: off, cross-encoder (local fastembed) or llm (role reranker) | rerank.py |
| `COMMONS_RERANK_MAX_TOKENS` | `3000` | Token budget for the LLM reranker prompt | rerank.py |
| `COMMONS_RERANK_MODEL` | `Xenova/ms-marco-MiniLM-L-6-v2` | Cross-encoder model (default Xenova/ms-marco-MiniLM-L-6-v2) | rerank.py |
| `COMMONS_RERANK_TOP` | `30` | How many top hits the reranker reorders | bench.py, store.py |
| `COMMONS_RESOLVE_DATES` | — | Set to 0 to stop annotating relative time words in results with absolute dates from each memory's own date ("yesterday (7 May 2023)") | bench.py, temporal.py |
| `COMMONS_RETAIN_*` | — | Retention days per table for `commons prune`, e.g. `COMMONS_RETAIN_USAGE=90` |  |
| `COMMONS_RETRO_AUTO_APPROVE` | — | Import retro-learning findings without review (`1`) | retro.py |
| `COMMONS_RETRO_AUTO_MIN_CONF` | `0` | Minimum confidence for auto-approved retro findings | retro.py |
| `COMMONS_RETRO_BUDGET` | `1.0` | USD budget for an LLM-assisted retro-learning run | retro.py |
| `COMMONS_RETRO_CHUNK_CHARS` | `12000` | Transcript chunk size for retro learning | retro.py |
| `COMMONS_RETRO_HOME` | — | Home directory scanned for past AI transcripts (default your home) | retro.py |
| `COMMONS_ROUTING_LEARN_N` | `5` | Outcomes needed before routing trusts a learned agent/model/effort score | routing.py |
| `COMMONS_ROUTING_MODE` | `balanced` | Execution-target routing: `balanced`, `quality`, `cheap`, `fast` | routing.py |
| `COMMONS_ROUTING_VALIDATE_N` | `5` | A/B samples before a routing proposal is validated | providers.py |
| `COMMONS_SKILL_AUTO_APPROVE` | — | Activate clean skill versions without a second agent (single-user setups only) | server_v4.py, skillpool.py |
| `COMMONS_SKILL_DISCOVERY` | `1` | Background skill discovery from public catalogs (`0` off) | background.py |
| `COMMONS_SQLITE_READERS` | `0` | Set to 1 to give each worker thread its own SQLite read connection (parallel reads under WAL). Default 0: reads share the writer connection; on an oversubscribed 8-core Mac the per-thread readers measured slower, so it stays opt-in until measured on your host | store.py |
| `COMMONS_SSE_MAX_SECONDS` | `3600` | Max lifetime of a dashboard/agent SSE stream | dashboard.py |
| `COMMONS_TLS_CERT` | — | TLS certificate (PEM) for HTTP transport and dashboard | cli.py, connect.py, dashboard.py, server.py |
| `COMMONS_TLS_KEY` | — | TLS private key (PEM) | cli.py, dashboard.py, server.py |
| `COMMONS_TOOLS` | — | Which tools MCP clients list: `core` (default, ~20 routine tools plus `tools`/`call` to reach the rest), `all`, or `core,name,...`. Per-agent: `commons agent tools NAME PROFILE`; per-connection: `?tools=` on the MCP URL | cli_v7.py, toolprofile.py |
| `COMMONS_TREE_SITTER` | `1` | Use tree-sitter for JS/TS code indexing (`0` = regex fallback) | codeindex.py |
| `COMMONS_TRUST_WEIGHTING` | `1` | Weight search/KB ranking by author trust (`0` off) | bench.py, store.py |
| `COMMONS_TYPESAFE_MODEL` | `jev-latest` | TypeSafe model (default jev-latest) | typesafe.py |
| `COMMONS_TYPESAFE_URL` | `https://api.typesafe.ai/v1/systemone` | TypeSafe System One endpoint (default https://api.typesafe.ai/v1/systemone) | typesafe.py |
| `COMMONS_URL` | — | Commons server base URL for hooks/clients (empty = local DB) | capture.py, cli.py |
| `COMMONS_WAKE_BACKOFF_SCALE` | `1` | Multiplier for wake-delivery retry backoff | messaging.py |
| `COMMONS_WAKE_PER_MIN` | `30` | Wake deliveries per agent per minute (excess coalesced) | messaging.py |
| `COMMONS_WAKE_PUSH_WHEN_LIVE` | `0` | Also push wakes to agents already connected live (`1`) | messaging.py |
| `COMMONS_WAKE_SYNC` | — | Deliver wakes synchronously (tests/debugging) | messaging.py |
| `COMMONS_WEBHOOK_ALLOWLIST` | — | Hosts allowed for webhooks/A2A push even if private or non-https (`*.example.com` ok) | messaging.py |
