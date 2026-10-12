# Connecting clients (detailed)

Moved from the README in v0.7.1.

## Connect page and `commons connect <platform>` (v0.8.1)

The dashboard's **Connect** page is the fastest path. It generates, for the chosen client:

| Block | Example (Cursor) |
|---|---|
| config / install | `~/.cursor/mcp.json` with `url` and `Authorization: Bearer <key>` |
| prompt | the [universal prompt](../PROMPT.md) with URL, agent name, project and key filled in |
| skill | `pip install commons-memory && commons install-skill cursor --agent NAME` |
| verify | a bash + curl one-liner: `initialize`, then `tools/call whoami`; prints `OK` or `FAIL` |

Platforms: `claude-code`, `codex`, `cursor`, `gemini`, `chatgpt` (any streamable-HTTP MCP client), `any` (prompt
plus plain-HTTP fallback), `docker` (self-host). Aliases: `claude`, `codex-cli`, `gemini-cli`, `http`, `generic`,
`self-host`.

**URL detection.** `COMMONS_PUBLIC_URL` if set. Otherwise, a wildcard MCP bind (`0.0.0.0`) is reached at the host the
browser used for the dashboard; a loopback bind gives `127.0.0.1`; a specific bind address is used as is. The port is
the MCP port (`COMMONS_MCP_PORT`, set by `commons serve`). The field is editable.

**Key creation** is allowed for admin users of an authed dashboard, or on a tokenless dashboard bound to a private
address (loopback, RFC1918, link-local, Tailscale 100.64/10) when the browser is on a private address too. Viewers
and public tokenless setups get the CLI hint instead. The key is shown once; only its hash is stored. With a project,
the role (read/write/admin) is granted as a project ACL.

**CLI and API.**
```sh
commons connect cursor --name laptop-cursor --new-key --project shop   # human-readable
commons connect codex --json                                           # same JSON as GET /api/connect?platform=codex
```
`GET /api/connect_status?name=NAME` returns `last_seen`/`connected`; the page polls it every 3 s.

**Troubleshooting.** 401: missing/wrong key. Unreachable: network (Tailscale `--accept-routes` or a subnet route
for LAN addresses, firewall, wrong IP). Unknown tool / schema mismatch: restart the client. Never gets work: run
`commons worker` or add a wake channel (`commons connect NAME --wake webhook:url=…`).

## Basics (v0.1)

## Quickstart

```bash
# install (pick one)
uv tool install git+https://github.com/Neoo-Blue/commons-memory
pip install git+https://github.com/Neoo-Blue/commons-memory

commons init                      # creates ~/.commons/commons.db, prints client config snippets
commons index . --project myapp   # index a repo into the graph
commons remember "Payments use Stripe webhooks; retries are idempotent" --project myapp
commons search "stripe retries"
commons export-graph --format html -o graph.html   # open in a browser
commons serve                     # MCP over stdio
commons serve --transport http --port 8765         # MCP streamable HTTP at http://127.0.0.1:8765/mcp
```

Optional better embeddings: `pip install "commons-memory[embed] @ git+https://github.com/Neoo-Blue/commons-memory"` and set
`COMMONS_EMBEDDER=fastembed` (BAAI/bge-small, local ONNX). Default is a dependency-free hashing embedder;
`COMMONS_EMBEDDER=none` gives keyword-only.

## Connect your AIs

All clients should point at the **same `COMMONS_DB`** (stdio) or the same HTTP server, so they share memory.

**Claude Desktop** — `claude_desktop_config.json`
```json
{
  "mcpServers": {
    "commons": {
      "command": "commons",
      "args": ["serve"],
      "env": { "COMMONS_AGENT": "claude-desktop", "COMMONS_DB": "/Users/you/.commons/commons.db" }
    }
  }
}
```

**Claude Code**
```bash
claude mcp add commons -e COMMONS_AGENT=claude-code -- commons serve
```

**Cursor** — `.cursor/mcp.json` (or `~/.cursor/mcp.json`)
```json
{
  "mcpServers": {
    "commons": { "command": "commons", "args": ["serve"], "env": { "COMMONS_AGENT": "cursor" } }
  }
}
```

**Generic / remote (streamable HTTP)** — ChatGPT connectors, custom agents, other MCP clients
```bash
commons register-agent my-agent --vendor acme     # prints an api_key (shown once)
commons serve --transport http --host <tailnet-or-LAN-ip> --port 8765
```
```json
{ "mcpServers": { "commons": { "url": "http://your-host:8765/mcp" } } }
```
Over HTTP, writes require the agent's `api_key` argument (set `COMMONS_REQUIRE_KEYS=0` or `--no-auth` for local dev).
For stdio you can set `COMMONS_API_KEY` in the client env instead of passing it per call. Put the HTTP server
behind TLS (reverse proxy) before exposing it beyond localhost.
