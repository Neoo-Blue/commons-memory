# Networking and remote access

By default Commons listens on localhost. `commons expose` makes it reachable from other places, and every option keeps authentication on.

```bash
commons serve --transport http --port 8765 &        # keys are required in HTTP mode
commons expose up tailscale                          # https://<node>.<tailnet>.ts.net (tailnet only)
commons status                                       # provider, URL, health (401 = protected and alive)
commons expose snippets                              # client configs for the reachable URL (also in ~/.commons/connect/)
commons expose down
```

| Provider | Command | Reach | Notes |
|---|---|---|---|
| Tailscale serve | `expose up tailscale` | Your tailnet | Runs `tailscale serve --bg --https=443 http://HOST:PORT`. The URL comes from `tailscale status --json`. |
| Tailscale bind | `expose up tailscale --https-port 0` / `expose bind tailscale` | Your tailnet | Prints the tailnet IP to pass to `serve --host`. No proxy. |
| Tailscale Funnel | `expose up tailscale --public` | **Internet** | `tailscale funnel --bg`. Prints a warning. Auth is required. |
| Cloudflare quick | `expose up cloudflare` | **Internet** | `cloudflared tunnel --url`. Random `trycloudflare.com` URL with no uptime guarantee. |
| Cloudflare named | `expose up cloudflare --name N --hostname mem.example.com --token-file F` | **Internet** | Add Cloudflare Access in front. Commons validates the Access JWT (below). |
| ngrok | `expose up ngrok [--oauth google --oauth-allow you@x.com] [--cidr-allow 1.2.3.0/24] [--domain d.ngrok.app]` | **Internet** | Uses the agent CLI if present, otherwise the `ngrok` Python SDK (`NGROK_AUTHTOKEN`). |
| SSH (localhost.run, serveo, your own box) | `expose up ssh [--ssh user@host]` | **Internet** | `ssh -R 80:HOST:PORT nokey@localhost.run` |
| bore | `expose up bore --allow-plain-http [--domain bore.example]` | **Internet** | Plain TCP, no TLS. Refused unless you opt in. |
| ZeroTier / WireGuard | `expose up zerotier` / `expose up wireguard --iface wg0` | Overlay network | Binds the overlay IP. |
| Reverse proxy | `expose proxy caddy --domain mem.example.com` / `expose proxy nginx …` | Your domain | Caddy handles TLS automatically. The nginx config disables buffering for SSE. |

**Safety.** For any internet-facing provider, `expose up` first checks the target. It POSTs to `/mcp` without credentials and must get a **401**. It also checks that `COMMONS_REQUIRE_KEYS` isn't `0` and anonymous reads are off. If any check fails it refuses to start. Tunnel client output goes to `~/.commons/expose-<tool>.log`.

**Cloudflare Access.** Set `COMMONS_CF_ACCESS_TEAM=<team>` and `COMMONS_CF_ACCESS_AUD=<application AUD tag>`. Requests that arrive through Cloudflare (they carry `cf-ray`) must then include a valid `Cf-Access-Jwt-Assertion`, checked as RS256 against `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs` with that audience. Otherwise they get a 403, even when they carry an agent key. Service tokens are accepted, since Access turns them into a JWT with `common_name`. With `COMMONS_CF_ACCESS_AS_AUTH=1`, a valid Access identity can stand in for an agent key, as agent `cf:<email or common_name>`. Direct tailnet or localhost requests are unaffected.

**Connection snippets.** `~/.commons/connect/` holds snippets for Claude Code, Cursor, Claude Desktop (via mcp-remote), Codex, Gemini CLI, a worker and the A2A card. They are rewritten on every `expose up` or `down`, so they always point at the reachable URL. Keys stay as `${COMMONS_API_KEY}` placeholders.

## Running as a service

`commons expose service systemd|launchd|windows` prints a unit. Ready-made copies are in `deploy/`:
- systemd (user unit, hardened): `~/.config/systemd/user/commons.service`, then `systemctl --user enable --now commons`
- launchd: `~/Library/LaunchAgents/dev.commons.server.plist`, then `launchctl load -w …`
- Windows: NSSM or WinSW (`deploy/windows-service.cmd`), or a logon scheduled task

## Docker

```bash
docker build -t commons-memory .
docker run -d --name commons -p 127.0.0.1:8765:8765 -p 127.0.0.1:8766:8766 -v commons:/data \
  -e COMMONS_DASHBOARD_TOKEN=$(openssl rand -hex 16) commons-memory
docker exec commons commons register-agent claude-code
docker compose up -d                       # SQLite
docker compose --profile postgres up -d    # + pgvector (set COMMONS_DB to the postgres DSN)
docker compose --profile tunnel up -d      # + cloudflared named tunnel (TUNNEL_TOKEN)
```

### Publishing the image

```bash
echo $GITHUB_TOKEN | docker login ghcr.io -u <user> --password-stdin
docker buildx build --platform linux/amd64,linux/arm64 -t ghcr.io/neoo-blue/commons-memory:0.6.0 \
  -t ghcr.io/neoo-blue/commons-memory:latest --push .
```
