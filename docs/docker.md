# Docker

One line (SQLite, data in a named volume, ports only on localhost):

```bash
docker run -d --name commons --restart unless-stopped -p 127.0.0.1:8765:8765 -p 127.0.0.1:8766:8766 -v commons-data:/data ghcr.io/neoo-blue/commons-memory:latest
```

Then:

```bash
docker exec commons commons register-agent claude-code   # prints the agent's MCP key once
docker exec commons cat /data/dashboard.token            # dashboard: http://127.0.0.1:8766/?token=<token>
```

MCP endpoint: `http://127.0.0.1:8765/mcp` (agent key required, `Authorization: Bearer <key>`); agent-bus shim: `/bus/*`;
A2A: `/a2a`. Podman works the same (`podman run ...`, rootless is fine).

## Compose

```bash
curl -O https://raw.githubusercontent.com/Neoo-Blue/commons-memory/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/Neoo-Blue/commons-memory/main/.env.example
mkdir -p docker secrets && chmod 700 secrets
openssl rand -hex 24 > secrets/dashboard_token && openssl rand -hex 24 > secrets/postgres_password && chmod 644 secrets/*
docker compose up -d                          # SQLite
docker compose --profile postgres up -d       # Postgres 16 + pgvector; set COMMONS_DB in .env (see .env.example)
docker compose --profile tailscale up -d      # + Tailscale sidecar (TS_AUTHKEY in .env; needs docker/tailscale-serve.json)
docker compose --profile cloudflared up -d    # + Cloudflare named tunnel (CLOUDFLARE_TUNNEL_TOKEN)
docker compose --profile ngrok up -d          # + ngrok (NGROK_AUTHTOKEN, optional NGROK_FLAGS for OAuth/IP limits)
```
(From a clone everything is already in place; `docker compose build` builds locally.)

## The image
- Multi-stage, `python:3.12-slim`, non-root user `commons` (uid 10001), `tini` as PID 1 (clean SIGTERM, zombie reaping),
  healthcheck on `/bus/health`, `linux/amd64` + `linux/arm64`.
- Everything persistent lives in `/data`: `commons.db`, `drive/` (Commons Drive blobs), `backups/`, `dashboard.token`,
  model caches. Back up the volume (or use `commons backup`).
- Extras included: postgres, http, tokens, code (tree-sitter), otel, sftp, smb, ws, embed (fastembed).
- Config is env only: `COMMONS_DB`, `COMMONS_REQUIRE_KEYS` (always 1 in the image), `COMMONS_PUBLIC_URL`,
  `COMMONS_WEBHOOK_ALLOWLIST`, `COMMONS_DRIVE_*`, `COMMONS_DASHBOARD_TOKEN`... Any `X_FILE=/run/secrets/...` variable is
  loaded into `X` at start (docker secrets). Provider/API keys: pass secret references (`env://`, `vault://`, `op://`).
- Postgres: the entrypoint waits up to `COMMONS_DB_WAIT` (60) seconds for the database.
- Compose hardening: read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`.

## Dashboard token inside containers
Inside the container the servers bind `0.0.0.0`, which the dashboard treats as public, so the image always uses a token:
`COMMONS_DASHBOARD_TOKEN(_FILE)` if given, otherwise one is generated and stored in `/data/dashboard.token`.
Compose publishes both ports to `127.0.0.1` by default (`COMMONS_BIND`); set `COMMONS_BIND` to a tailnet/LAN IP to reach it
from other machines, or use one of the tunnel profiles. MCP agent keys are always required regardless of bind address.

## Build / publish yourself
```bash
docker buildx build --platform linux/amd64,linux/arm64 --build-arg VERSION=0.7.2 \
  -t ghcr.io/<you>/commons-memory:0.7.2 -t ghcr.io/<you>/commons-memory:latest --push .
```
The GitHub Actions workflow in `ci/ghcr-publish.yml` does this on every `v*` tag (copy it to `.github/workflows/`).
