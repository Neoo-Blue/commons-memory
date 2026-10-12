#!/bin/sh
# Loads *_FILE env vars (docker secrets), then runs `commons`. `serve` (default) starts MCP + dashboard on 0.0.0.0.
set -e
for var in $(env | sed -n 's/^\([A-Z0-9_]*\)_FILE=.*/\1/p'); do
  file=$(printenv "${var}_FILE")
  if [ -r "$file" ] && [ -z "$(printenv "$var" || true)" ]; then
    export "$var=$(cat "$file")"
  fi
done
if [ -z "$COMMONS_DASHBOARD_TOKEN" ] && [ -r /data/dashboard.token ]; then
  COMMONS_DASHBOARD_TOKEN=$(cat /data/dashboard.token); export COMMONS_DASHBOARD_TOKEN
fi
case "$COMMONS_DB" in
  postgres://*|postgresql://*)  # wait for the database (compose starts services in parallel)
    python - <<'PY' || echo "database not reachable yet; starting anyway" >&2
import os, time, psycopg
dsn = os.environ["COMMONS_DB"].split("?")[0]
for i in range(int(os.environ.get("COMMONS_DB_WAIT", "60"))):
    try:
        psycopg.connect(dsn, connect_timeout=2).close(); break
    except Exception:
        time.sleep(1)
else:
    raise SystemExit(1)
PY
    ;;
esac
if [ "$1" = "serve" ] && [ "$#" -eq 1 ]; then
  if [ -z "$COMMONS_DASHBOARD_TOKEN" ]; then
    COMMONS_DASHBOARD_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(24))")
    umask 077; printf %s "$COMMONS_DASHBOARD_TOKEN" > /data/dashboard.token
    echo "dashboard token generated and saved to /data/dashboard.token (docker exec <ctr> cat /data/dashboard.token)" >&2
    export COMMONS_DASHBOARD_TOKEN
  fi
  set -- serve --transport http --host "${COMMONS_HOST:-0.0.0.0}" --port "${COMMONS_PORT:-8765}" \
    --dashboard --dashboard-host "${COMMONS_HOST:-0.0.0.0}" --dashboard-port "${COMMONS_DASHBOARD_PORT:-8766}"
fi
exec commons "$@"
