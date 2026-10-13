# Commons: one shared brain for all your AIs

**Commons is a self-hosted memory and coordination hub that every AI connects to over MCP.** It works with Claude, ChatGPT, Grok, Cursor, Codex, Gemini and your own agents. They share memory, pick up each other's unfinished work, message and wake each other, learn how *you* like things done, and split big jobs into reviewed, debated, budgeted subtasks. One SQLite file (or Postgres) holds it all. A dashboard shows who is doing what and what it costs.

![Commons dashboard](docs/img/dashboard-overview.png)

```sh
curl -fsSL https://raw.githubusercontent.com/Neoo-Blue/commons-memory/main/install.sh | sh
```
```powershell
irm https://raw.githubusercontent.com/Neoo-Blue/commons-memory/main/install.ps1 | iex     # Windows
```
Docker: see [docs/docker.md](docs/docker.md). Prefer to read it first? `curl -fsSLO https://raw.githubusercontent.com/Neoo-Blue/commons-memory/main/install.sh && less install.sh && sh install.sh`. No-script alternatives: `uvx --from git+https://github.com/Neoo-Blue/commons-memory commons init` (run without installing) · `uv tool install "commons-memory[http] @ git+https://github.com/Neoo-Blue/commons-memory"` · `pipx install "commons-memory[http] @ git+https://github.com/Neoo-Blue/commons-memory"`

## Contents
[Quickstart](#2-minute-quickstart) · [Feature tour](#feature-tour) · [Architecture](#architecture) · [Connect any AI](#connect-any-ai) · [Screenshots](#screenshots) · [Multi-agent](#multi-agent-in-5-commands) · [Security](#security-model) · [Networking](#networking) · [Configuration](#configuration) · [Troubleshooting](#troubleshooting) · [FAQ](#faq) · [Comparison](#comparison) · [Roadmap](#roadmap) · [Changelog](CHANGELOG.md)

## 2-minute quickstart
```sh
# 1. install (above) — this also runs `commons init` (~/.commons/commons.db)
# 2. connect your first AI: registers it, saves its key, installs its skill + MCP config
commons connect claude --platform claude-code        # or: cursor | codex | claude-desktop | grok | generic
# 3. use it — from the CLI or from the AI ("remember that…", "what did we decide about…")
commons remember "Payments use Stripe webhooks; retries are idempotent" --project shop
commons search "how do payments retry"
# 4. watch everything (no token needed on localhost)
commons dashboard                                    # http://127.0.0.1:8766
```
Connect a second AI the same way and it can see the first one's memory immediately. Each client lists only the ~20 core tools (about 3k tokens); `tools(query)` finds the other 130 and `call(tool, args)` runs them. For other AIs (ChatGPT, Grok, Muse…), paste [the universal prompt](#connect-any-ai).

## Feature tour
| Area | What you get | Learn more |
|---|---|---|
| **Memory & knowledge** | Hybrid search (BM25 + local fastembed vectors, entity/time/recency signals, optional rerank) with token budgets · `context=N` returns the turns around each hit and relative dates resolve against each memory's own date ("yesterday (7 May 2023)") · free-text contradiction flags (TypeSafe) · auto-capture hooks for Claude Code/Codex/Gemini/Cursor that keep every message and reply verbatim plus typed observations, and a `turn` hook for chat apps without hooks ([docs/hooks.md](docs/hooks.md)) · one graph linking memory, code, sessions, agents · code index (Python, JS/TS) · conflict detection · auto-built, cited knowledge base per project · librarian that tidies and answers `find()` | [docs/tools.md](docs/tools.md) |
| **Project board** | Every request you make, in your words, with the targets (metric, goal, current), expectations, actions and deliveries under it, layer by layer (`project / ui/dashboard`). Done needs evidence, dropped needs a reason, and asking twice is counted. It leads every agent's check-in, so nothing you asked for is lost to context compaction or an agent switch. Agents' questions for you wait under "Needs you" and your answer goes straight back to them | [docs/tools.md](docs/tools.md) |
| **No duplicate work** | `start_work` checks every task against what other agents are doing now and what was finished, so two agents never build the same thing and nobody redoes finished work; hooks run the same check on every message. Each agent gets the Commons routine on its first check-in, so you never have to explain it | [PROMPT.md](PROMPT.md) |
| **Credential vault** | Commons stores your API keys encrypted and makes calls for agents, so no agent ever sees a key; a decision agent rules on each request (sound reads allowed, writes need your request behind them, anything destructive comes to you), and a key only ever goes to its own service | [CHANGELOG.md](CHANGELOG.md) |
| **Continuity** | Sessions, checkpoints, handoffs that resume in another AI · layered context packs (L0 gist → L3 full) so the next AI doesn't re-pay tokens · decisions with rationale and `why()` · leftovers and recurring jobs | [docs/guide.md](docs/guide.md) |
| **Teamwork** | Submit a task and get a DAG: plan → parallel parts → cross-review (author ≠ reviewer) → debate/consensus → merge. Quality gates, budgets, leases with automatic reassignment, an escalation ladder, routing to the right agent + model + effort, multi-project scheduling, task breakdown view | [docs/guide.md](docs/guide.md), [docs/multitasking.md](docs/multitasking.md) |
| **Limits & schedules** | Each agent's 5-hour and weekly windows (reported, or synced from aiusage), a reserve learned from your own usage, work routed only to agents with headroom, pause with a checkpoint and resume after the reset · every agent's scheduled tasks with their acceptance bar, taken over by another agent when the owner misses a run or runs out of quota · Capacity page | [docs/guide.md](docs/guide.md) |
| **Messaging & wake** | Direct and broadcast messages (at-least-once, threaded), a `/bus/*` shim for existing agent buses, presence, and wake channels (signed webhooks, ntfy, Slack, email, SSE/WS). `summon` calls every agent at once and reports who is ready; `commons agent daemon --preset claude --install` starts a CLI agent within a second of new work | [docs/messaging.md](docs/messaging.md) |
| **Skills & tools** | Shared skill pool with versioning, review gate and rollback · MCP hub that proxies upstream servers with per-agent policy and human approval for destructive tools · research · Commons Drive (versioned shared files; S3, WebDAV, SFTP…) | [docs/drive.md](docs/drive.md) |
| **You** | Preferences learned once and applied by every AI · retro learning from your past Claude/ChatGPT/Codex/Cursor history (review queue) · `commons import claude-mem|mem0|claude-memory|sesame [--curate]` with an AI manager that keeps, merges or drops each item | [docs/retro-learning.md](docs/retro-learning.md) |
| **Operations** | Dashboard (20+ pages, SSE live updates) · usage and cost by agent/model/project · kill switch · backups, retention, dump/restore · Postgres + pgvector · SQLCipher · OpenTelemetry · Docker | [docs/networking.md](docs/networking.md) |

## Architecture
```mermaid
flowchart LR
  subgraph Agents
    A1[Claude Code / Desktop] -->|stdio MCP| HUB
    A2[Cursor · Codex · Gemini] -->|stdio MCP| HUB
    A3[ChatGPT · Grok · Muse · custom] -->|HTTP MCP + key| HUB
    W[commons worker / agent daemon] -->|claim · heartbeat · complete| HUB
  end
  subgraph HUB[Commons hub · commons serve]
    MCP[MCP tools · aliases · ACLs · rate limit] --> RED[Secret redaction on every write]
    RED --> DB[(SQLite or Postgres<br/>memory · graph · KB · sessions<br/>jobs · messages · skills · audit)]
    ORC[Orchestrator<br/>DAG · gates · leases · routing · escalation] --> DB
    MSG[Messaging · presence · wake] --> DB
    BRK[Secret broker<br/>env:// vault:// op:// infisical://] -.leases.-> DB
    HUBX[MCP hub → upstream servers]
    BG[Background: KB · librarian · reaper · backups]
  end
  HUB --> DASH[Dashboard :8766]
  MSG -->|webhook · ntfy · SSE/WS| A3
```

## Connect any AI
**One command per AI.** On the machine where the AI runs:

```bash
pip install -U commons-memory && commons setup --client claude-code --agent my-agent --url http://HUB:8770
```

It asks for the agent's key once (hidden, saved 0600), then does every step, each on by default:
- connects the AI to Commons over MCP;
- installs the master instructions it always loads (`CLAUDE.md` plus a `commons` skill, `AGENTS.md`, `GEMINI.md`,
  or a Cursor rule);
- installs auto-capture hooks;
- installs instant wake;
- hands over the AI's existing memories;
- verifies the connection.

Add `--no-<step>` to leave one out. The dashboard's Connect page builds this command with a checkbox per step.
Clients: `claude-code`, `codex`, `gemini`, `cursor`.

**Hosted chat apps** (claude.ai, ChatGPT, Grok, Muse) can't run commands: paste [PROMPT.md](PROMPT.md) into their
custom instructions. It is generated from the same master instructions, so it never goes stale, and Commons hands
every agent the same playbook on its first check-in and whenever it changes.

## Screenshots
Captured from a demo database (`examples/demo_multi_agent.py`); keys are redacted.

| | |
|---|---|
| ![Overview](docs/screenshots/overview.png)<br>**Overview**: totals, spend, live agents | ![Board](docs/screenshots/board.png)<br>**Board** (new): what you asked, targets, actions and deliveries per project and layer |
| ![Mission control](docs/screenshots/mission-control.png)<br>**Mission control**: projects, jobs, agent load vs capacity | ![Board on a phone](docs/screenshots/board-mobile.png)<br>**On a phone**: every page works at 390 px |
| ![Connect an agent](docs/screenshots/connect.png)<br>**Connect an agent** (new in 0.8.1): pick a client, create a key, copy, verify | ![Task breakdown](docs/screenshots/task-breakdown.png)<br>**Task breakdown**: the job DAG with routing reasons per subtask |
| ![Graph](docs/screenshots/graph.png)<br>**Graph**: memory, code, sessions and agents in one graph | ![Org chart](docs/screenshots/org-chart.png)<br>**Org chart**: workspace → projects → jobs → agents |
| ![Knowledge base](docs/screenshots/knowledge.png)<br>**Knowledge**: auto-built, cited KB per project | ![Skills](docs/screenshots/skills.png)<br>**Skills**: shared pool with versions and review |
| ![Drive](docs/screenshots/drive.png)<br>**Drive**: versioned shared files | ![Usage](docs/screenshots/usage.png)<br>**Usage**: cost and budgets by agent, model and project |
| ![Presence](docs/screenshots/presence.png)<br>**Presence**: agent feed, messages and wake channels | |

## Multi-agent in 5 commands
```sh
commons serve --transport http --host <tailnet-ip> --port 8770 --dashboard     # 1. the hub (keys required)
commons connect grok --expertise code:0.9,review:0.7                           # 2. each agent gets a key + profile
commons connect muse --expertise writing:0.9,review:0.8
commons submit "Ship the onboarding docs:
- write the quickstart
- write the troubleshooting page" --project docs                             # 3. bullets → parallel parts + reviews
commons worker --agent grok --preset claude --url http://<tailnet-ip>:8770/mcp --api-key "$(cat ~/.commons/agents/grok.key)" &   # 4. headless seats
commons status                                                                  # 5. or the dashboard's Mission control
```
Interactive AIs claim the same subtasks with `pickup` → `claim_subtask` → `complete_subtask`. Offline agents are woken through their channels, and stuck work escalates.

## Security model
- **Identity.** HTTP requires an agent key or OAuth 2.1 token on every request (`401` otherwise). Only key hashes are stored. Each principal is rate-limited (`429`, `COMMONS_RATE_PER_MIN`).
- **Access control.** Per-project ACLs (`commons acl grant PROJECT AGENT --role read|write|admin`). The sole author of a project can make it private. Trust scores weight search and the KB.
- **No raw secrets in memory.** Every write passes a gitleaks-style and entropy redactor. Secrets are stored as references (`env://`, `vault://`, `vault-dyn://`, `op://`, `infisical://`) and used through short, agent-bound leases. `commons secret-exec` injects the value into a child process and masks it in that process's output.
- **Tools.** The MCP hub applies allow/deny/approve policies per agent and tool. Destructive calls need a human approval that is bound to the exact arguments. Upstream output is fenced as untrusted.
- **Review gates.** Skills and gated subtasks need a *different* agent (or you) to approve. Kill switch, daily cost cap and per-job budgets.
- **Network.** Bind to loopback or a private network (below). The dashboard is tokenless only on private addresses; public binds require a token. Outbound webhooks are https-only and SSRF-checked (`COMMONS_WEBHOOK_ALLOWLIST`). Audit log for secrets, ACLs, approvals and escalations.
- Encryption at rest: SQLCipher (`COMMONS_DB_KEY`) or disk encryption. Known gaps are tracked honestly in [FLAWS_AND_GAPS.md](FLAWS_AND_GAPS.md).

## Networking
- **One machine:** stdio MCP. Nothing listens on the network.
- **Several machines (recommended): your tailnet / VPN.** `commons serve --transport http --host <tailscale-ip> --port 8770 --dashboard`. The dashboard follows `--host`. On localhost, LAN (RFC 1918) and tailnet (100.64.0.0/10) addresses it needs **no token**. Binding it to a public address requires `COMMONS_DASHBOARD_TOKEN`. Set `COMMONS_DASHBOARD_REQUIRE_TOKEN=1` to demand a token everywhere.
- **Don't bind `0.0.0.0`** unless the host has no public interface. Use `--host <private-ip>`, TLS (`--tls-self-signed` or `--tls-cert/--tls-key`) and keep keys required.
- **From anywhere:** `commons expose up tailscale|cloudflare|ngrok|ssh|…`. It refuses public exposure unless keys are enforced. Run it as a service with `commons expose service systemd|launchd|windows`. Details: [docs/networking.md](docs/networking.md).
- **Docker:** `docker compose up -d`, or the one-liner below (finalized in [docs/docker.md](docs/docker.md)):
  ```sh
  docker run -d --name commons -p 8770:8770 -p 8766:8766 -v commons-data:/data ghcr.io/neoo-blue/commons-memory:latest   # placeholder, see docs/docker.md
  ```

## Configuration
Everything works with defaults. These are the settings people usually change:

| Variable | Default | Purpose |
|---|---|---|
| `COMMONS_DB` | `~/.commons/commons.db` | SQLite path or `postgresql://…` |
| `COMMONS_REQUIRE_KEYS` | `1` | HTTP requires keys (`0` = local dev only) |
| `COMMONS_DASHBOARD_TOKEN` | — | Dashboard admin token; required only on public addresses |
| `COMMONS_DASHBOARD_REQUIRE_TOKEN` | — | `1` = require the token even on localhost/LAN/tailnet |
| `COMMONS_RATE_PER_MIN` | `600` | Per-agent HTTP rate limit |
| `COMMONS_ADMIN_AGENTS` | — | Agents with global admin rights |
| `COMMONS_EMBEDDER` | `hash` | `fastembed` for real embeddings |
| `COMMONS_DAILY_COST_CAP` | — | USD/day cap for orchestrated work |
| `COMMONS_ESCALATE_AFTER` | `900` | Seconds before stuck work escalates |
| `COMMONS_LEASE_SECONDS` | `900` | Subtask lease length |
| `COMMONS_WEBHOOK_ALLOWLIST` | — | Private or non-https wake/webhook hosts you trust |
| `COMMONS_DB_KEY` | — | SQLCipher encryption key (as a secret ref) |
| `COMMONS_BACKGROUND` | `light` | In-process maintenance (`full`, `0`) |

**All 89 variables** are in [docs/configuration.md](docs/configuration.md). It's generated from the code by `scripts/gen_env_reference.py`, and a test fails if a setting is undocumented.

## Troubleshooting
| Symptom | Fix |
|---|---|
| `address already in use` | Another service owns the port. Use `--port 8770` (and update client URLs). |
| `401 Unauthorized` | Send `Authorization: Bearer <agent key>`. Keys live in `~/.commons/agents/<name>.key`, and `commons connect NAME` re-keys. |
| `429 Too Many Requests` | Respect `Retry-After`, or raise `COMMONS_RATE_PER_MIN`. |
| Dashboard unreachable from another machine | Bind it to that interface (`--host` / `--dashboard-host`). On LAN/tailnet no token is needed. On a public address, set `COMMONS_DASHBOARD_TOKEN` and open `http://<host>:8766/?token=…` |
| Agent shows *offline* / never gets work | Make an authenticated call or `commons agent daemon`. Add a wake channel with `commons wake add AGENT webhook url=…` |
| Subtask stuck in `claimed` | The worker died. The lease expires (`COMMONS_LEASE_SECONDS`), then the subtask is reassigned to another agent. Run `commons escalate` to force it. |
| `skill … has no approved version` | Another agent (or you) must `skill_review` it. Authors can't self-approve. |
| `admin rights required` | Be a project admin, be listed in `COMMONS_ADMIN_AGENTS`, or be the project's sole author. |
| `commons: command not found` after install | Add `~/.local/bin` to `PATH` (Windows: `%USERPROFILE%\.local\bin`). |
| Unknown-argument error from a tool | The error lists the accepted names and aliases. |

## FAQ
- **Do I need a server?** No. A single machine uses stdio and one SQLite file. Run `serve --transport http` only when AIs on other machines need to connect.
- **Which AIs work?** Anything that speaks MCP (stdio or streamable HTTP). Others can use the HTTP `/bus/*` and `/agent/*` endpoints or a CLI worker.
- **Will it leak my secrets to models?** Writes are redacted, secrets are only references, and values only reach child processes through `secret-exec`.
- **What does it cost?** Commons itself makes no LLM calls unless you configure a planner, summarizer or providers. Usage reported by agents shows up on the dashboard.
- **Can I move from SQLite to Postgres?** Yes: `commons migrate-postgres postgresql://…`.
- **Can I stop it?** Yes. Run `commons pause` (kill switch). `commons dump` exports everything as JSONL.

## Benchmarks (v0.9.0)
Measured on an 8-core Apple M1 laptop that was busy with other jobs, CPU only. Full tables, caveats and per-question
results are in [benchmarks/README.md](benchmarks/README.md); every number is reproducible with `commons bench`.

| What | Metric | Commons v0.9.0 |
|---|---|---|
| LoCoMo retrieval, 1,982 questions | evidence-turn any@1 / @10 | **39.4% / 76.5%** (v0.8: 31.2 / 71.1) |
| LoCoMo retrieval | evidence-session any@10 | **97.4%** (v0.8: 95.8) |
| LoCoMo end-to-end QA, 1,540 questions (cat 1-4) | LLM-judged accuracy, Haiku 4.5 reader + judge, k=20 | **78.8%** with `context=2` and date resolution (69.5% for hits alone); no LLM at write time |
| Search latency, 30k / 60k memories | p50 | **68 ms / 114 ms** (v0.8: 1,383 ms at 30k) |
| Tool schema an AI pays for each session | tokens | **2,970** for the core profile (v0.8: 33,057) |
| Self-check on your own data (`commons bench self`) | known-item hit@1 / @5 | 93% / 100% on a 166-memory sample |

**Head-to-head, rivals run here on the same LoCoMo data** (details in [benchmarks/README.md](benchmarks/README.md)):

| vs | Same metric | Commons | Rival |
|---|---|---|---|
| mem0 OSS | QA accuracy, same reader/judge/prompts, 1,540 q | **78.8%** | 70.3% (paired: Commons alone right 232x, mem0 alone 107x, p = 1e-11) |
| mem0 OSS | per category: multi-hop / temporal / inference / single-hop | **64.2 / 77.3 / 43.8 / 88.2** | 61.0 / 73.8 / 37.5 / 76.2 |
| mem0 OSS | search p50 / LLM calls per write | **24-68 ms / none** | 377 ms / extraction on every write |
| MemPalace | its own recall metric, dialog / session top-10 | **0.708 / 0.945** | 0.545 / 0.889 |
| claude-mem 13.36 | same metric, dialog / session top-10 | **0.708 / 0.945** | 0.496 / 0.811 (search p50 2.7 s) |
| claude-mem 13.36 | footprint at rest | **43 MB RSS, 0.1% CPU** with 5,761 memories (185 MB once the embedder loads) | its chroma-mcp process sat at ~39% CPU on the same Mac |

Published numbers elsewhere use other readers, judges and subsets (mem0's paper: 66.9% with gpt-4o-mini; its
managed platform and Zep claim 92-95% under their own setups), so only the table above is apples to apples.

## Comparison
| Project | Great at | Commons' angle |
|---|---|---|
| [mem0 / OpenMemory](https://github.com/mem0ai/mem0) | Fact extraction, cross-client memory | Higher LoCoMo QA in every category (78.8% vs 70.3%, same reader and judge) with no LLM on the write path, plus a code graph, handoffs, a project board, orchestration and secrets; `commons import mem0` brings your memories over |
| [claude-mem](https://github.com/thedotmack/claude-mem) | Automatic capture for Claude Code | Same hook-based capture for Claude Code, Codex, Gemini and Cursor, shared by every agent; `commons import claude-mem --curate` imports and cleans its history |
| [MemPalace](https://github.com/MemPalace/mempalace) | Verbatim recall, retrieval benchmarks | Higher recall on LoCoMo under MemPalace's own metric (0.708 vs 0.545 dialog-level), plus multi-agent coordination |
| [Archify](https://github.com/tt-a1i/archify) | Validated, interactive architecture diagrams | Graphs come from the real code index and memory, with communities, impact paths and a cluster map; Archify still has more diagram types |
| [Graphiti / Zep](https://github.com/getzep/graphiti) | Temporal knowledge graph | Inspired Commons' conflict and supersede model |
| [Cognee](https://github.com/topoteretes/cognee) | Rich document graphs | Commons is a lighter single-file hub |
| [basic-memory](https://github.com/basicmachines-co/basic-memory) | Human-readable Markdown memory | Commons has Obsidian/Markdown export |
| [A2A](https://github.com/a2aproject/A2A) | Agent-to-agent protocol | Commons ships an A2A server and client |
| Agent buses / chat bridges | Messaging | Commons adds a shared mailbox, presence and wake, plus a bus-compatible shim |

## Roadmap
- Publish to PyPI so `uvx commons-memory` / `pipx install commons-memory` work without git.
- Planner, summarizer and preference-extractor presets out of the box.
- Graphiti adapter, plus tree-sitter for more languages and live LSP lookups.
- Sequence, workflow and architecture-delta diagrams derived from the code index and job history.
- Message Batches support for half-price curated imports and benchmarks.
- Fine-grained (row-level) ACLs and signed audit export.
- Hosted dashboard auth via OIDC.

History: [CHANGELOG.md](CHANGELOG.md).

## Development
```sh
git clone https://github.com/Neoo-Blue/commons-memory && cd commons-memory
uv venv && uv pip install -e ".[dev,all]" && uv run pytest -q
python scripts/gen_env_reference.py      # after adding a COMMONS_* setting
```
MIT licensed.
