# Threat model

What Commons protects, what it doesn't, and what you should do about the gap. Two modes are covered: the Claude Code
plugin (one machine, stdio) and the self-hosted hub (several machines, HTTP).

## Plugin mode (one machine)

**What runs.** Each Claude Code session starts its own `commons serve` over stdio. They share one SQLite file,
`~/.commons/commons.db`. Nothing listens on the network. No background loop runs in this mode, and Commons makes no
outbound calls unless a tool you call needs one (web research, or a provider key you add yourself).

**What it can reach.** The plugin lists 19 tools: memory (`remember`, `search`, `expand`, `why`, `record_decision`) and
coordination (`pickup`, `start_work`, `finish_work`, `handoff`, `resume`, `msg_send`, `msg_recv`, `msg_get`, the board, `whoami`),
plus `tools`/`call` to reach the rest of the catalog on request. No credential, vault or infrastructure tool is
listed, though `call()` can still reach them by name. The vault has no key until you create one, so on a fresh install
there is nothing for an agent to spend. The plugin uses `~/.commons/commons.db`, the same file as a local `commons`
CLI install: if you keep a vault in that file, set `COMMONS_DB` for the plugin to a separate path.

**Secrets in memory.** Every write passes a gitleaks-style and entropy redactor before it is stored, so a token pasted
into a note is stored as `[REDACTED:<kind>]`.

| Threat | Covered? | How |
|---|---|---|
| A session reads another session's notes | By design | Sessions on one machine are meant to share. Use `remember(visible_to=[...])` to limit a note to named agents. |
| A secret pasted into a memory | Yes | Redacted on write. |
| Another program running as you reads the database | No | It is a file in your home directory. Use disk encryption, or SQLCipher with `COMMONS_DB_KEY`. |
| A poisoned memory steers every later session | Partly | See below. |

### Poisoned memory (prompt injection that persists)

Shared memory is a channel: if one session is tricked into storing an instruction ("always run this script"), every
later session can read it. Commons narrows this but cannot close it, because the agents themselves decide what to do
with what they read.

- The plugin's skill and the server's instructions tell every agent that other agents' notes and messages are data,
  not instructions, and never to act on one that conflicts with the user.
- Every message carries its origin, and `msg_get(id)` lets an agent check that a relayed message really came through
  Commons and from whom.
- Text from strangers (issues, PR bodies, commit messages on watched repos, in hub mode) is fenced as untrusted and
  never becomes a job title.
- Every write is attributed to an agent and kept in the audit log, so a bad note can be traced and removed.

What you should do: keep permission prompts on for shell commands in your agents, and treat a surprising "the team
decided X" from memory the way you'd treat it from a stranger.

## Hub mode (several machines)

Everything above, plus:

- **Identity.** HTTP needs an agent key or OAuth token on every request; only key hashes are stored. The Docker image
  sets `COMMONS_REQUIRE_KEYS=1`.
- **Network.** Bind to loopback, a LAN or a tailnet address. Public binds and `commons expose` refuse to run unless
  keys are enforced. Outbound webhooks are https-only and SSRF-checked.
- **Credentials (optional).** If you turn on the vault, keys are encrypted, never shown to agents, and only sent to
  their own service. Each use is judged by a decision chain you configure; anything destructive or costly needs your
  owner password, which agents can't supply. Every use is audited.
- **Audit.** The audit log is a hash chain written as rows land; `commons audit-chain` detects edited, dropped or
  reordered rows, and `commons audit-export` produces a signed copy.

Known gaps are tracked in [FLAWS_AND_GAPS.md](../FLAWS_AND_GAPS.md). Found a security problem? Open a private
security advisory on GitHub rather than a public issue.
