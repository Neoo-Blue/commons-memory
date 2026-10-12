# Connecting clients (detailed)

Moved from the README in v0.7.1.

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
