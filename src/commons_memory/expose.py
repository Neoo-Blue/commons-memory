"""Networking / remote access: `commons expose`.

Providers
  local        bind 127.0.0.1 only (default; nothing to do)
  tailscale    bind the tailnet IP (auto-detected) and/or `tailscale serve --bg` (HTTPS on <node>.<tailnet>.ts.net,
               tailnet-only); `--public` uses `tailscale funnel --bg` instead (internet, warns, requires auth)
  cloudflare   `cloudflared tunnel --url` quick tunnel (random trycloudflare.com URL) or a named tunnel
               (`cloudflared tunnel run --token-file`/NAME + --hostname); optional Cloudflare Access JWT validation
               (COMMONS_CF_ACCESS_TEAM + COMMONS_CF_ACCESS_AUD) enforced in the HTTP middleware
  ngrok        ngrok agent CLI (`ngrok http`, with --oauth/--oauth-allow-email/--cidr-allow/--url passthrough) or the
               `ngrok` Python SDK when the CLI is missing
  ssh          localhost.run (`ssh -R 80:localhost:PORT nokey@localhost.run`) or any `user@host` reverse tunnel
  bore         `bore local PORT --to bore.pub` (plain TCP, no TLS: refused unless --allow-plain-http)
  zerotier / wireguard   bind the overlay interface IP (zerotier-cli listnetworks -j / `ip -4 addr show wg0`)
  proxy        prints a Caddy (automatic HTTPS) or nginx config for your domain

Safety: any public provider (funnel, cloudflare, ngrok, ssh, bore) refuses to start unless authentication is on: the
target answers 401 on /mcp without a key (probed live), COMMONS_REQUIRE_KEYS is not 0 and anonymous reads are off.
State (provider, URL, pid, public flag, health) lives in meta `expose:state`; `commons status`, the dashboard and the
connection snippets (~/.commons/connect/) use the reachable URL.
"""
from __future__ import annotations

import html
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .ids import now
from .store import CommonsError, Store

PROVIDERS = ("local", "tailscale", "cloudflare", "ngrok", "ssh", "bore", "zerotier", "wireguard", "proxy")
PUBLIC = {"cloudflare", "ngrok", "ssh", "bore"}


def _which(name: str) -> str | None:
    return shutil.which(name)


def _out(cmd: list[str], timeout: float = 20) -> str:
    exe = _which(cmd[0])
    if not exe:
        raise CommonsError(f"`{cmd[0]}` not found on PATH; install it first (see docs/networking.md)")
    r = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise CommonsError(f"{' '.join(cmd[:3])} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def _home() -> Path:
    return Path(os.environ.get("COMMONS_HOME") or Path.home() / ".commons")


# ------------------------------------------------------------------ detection
def tailscale_info() -> dict:
    """Self IPs + MagicDNS name from `tailscale status --json`."""
    d = json.loads(_out(["tailscale", "status", "--json"]))
    me = d.get("Self") or {}
    ips = me.get("TailscaleIPs") or []
    return {"ipv4": next((i for i in ips if "." in i), None), "ips": ips, "dns": (me.get("DNSName") or "").rstrip("."),
            "online": me.get("Online", True), "backend": d.get("BackendState")}


def zerotier_ip() -> str | None:
    for n in json.loads(_out(["zerotier-cli", "listnetworks", "-j"])):
        for a in n.get("assignedAddresses") or []:
            ip = a.split("/")[0]
            if "." in ip:
                return ip
    return None


def wireguard_ip(iface: str = "wg0") -> str | None:
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", _out(["ip", "-4", "addr", "show", iface]))
    return m.group(1) if m else None


def bind_host(provider: str, iface: str = "wg0") -> str:
    """Host to bind `commons serve` to for private-network providers."""
    if provider == "tailscale":
        ip = tailscale_info()["ipv4"]
    elif provider == "zerotier":
        ip = zerotier_ip()
    elif provider == "wireguard":
        ip = wireguard_ip(iface)
    else:
        ip = "127.0.0.1"
    if not ip:
        raise CommonsError(f"no {provider} address found; is it connected?")
    return ip


# ------------------------------------------------------------------ safety
def probe(url: str, timeout: float = 5, ctx=None) -> dict:
    """GET-less probe: POST {} to /mcp without credentials. Healthy+protected == 401."""
    u = url.rstrip("/") + ("" if url.rstrip("/").endswith("/mcp") else "/mcp")
    req = urllib.request.Request(u, data=b"{}", method="POST", headers={"Content-Type": "application/json",
                                                                        "Accept": "application/json, text/event-stream"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:  # noqa: S310 - our own server
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": 0, "error": str(e)[:200], "auth": None}
    return {"ok": code == 401, "status": code, "auth": code == 401, "ms": int((time.time() - t) * 1000)}


def require_auth(target: str, public: bool) -> dict:
    """Refuse public exposure unless the target demonstrably requires credentials."""
    p = probe(target)
    if not public:
        return p
    problems = []
    if os.environ.get("COMMONS_REQUIRE_KEYS", "1") == "0":
        problems.append("COMMONS_REQUIRE_KEYS=0")
    if os.environ.get("COMMONS_ALLOW_ANON_READS") == "1":
        problems.append("COMMONS_ALLOW_ANON_READS=1")
    if p["status"] == 0:
        problems.append(f"server not reachable at {target} ({p.get('error')}); start `commons serve --transport http` first")
    elif p["status"] != 401:
        problems.append(f"{target}/mcp answered {p['status']} without credentials (expected 401)")
    if problems:
        raise CommonsError("refusing public exposure, authentication is not enforced: " + "; ".join(problems))
    return p


# ------------------------------------------------------------------ state
def state(s: Store) -> dict:
    r = s.q("SELECT value FROM meta WHERE key='expose:state'")
    return json.loads(r[0]["value"]) if r else {"provider": "local", "url": None, "public": False}


def _save(s: Store, st: dict) -> dict:
    s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", ("expose:state", json.dumps(st)))
    s.audit(None, "expose.state", st.get("provider", ""), {k: st.get(k) for k in ("url", "public", "pid")})
    return st


def reachable_url(s: Store, default: str = "http://127.0.0.1:8765") -> str:
    st = state(s)
    return (st.get("url") or default).rstrip("/")


def record_serve(host: str, port: int, tls: bool = False) -> dict:
    """Written by `commons serve --transport http` so other processes (status, dashboard, the Docker healthcheck)
    know the real bind instead of assuming 127.0.0.1:8765."""
    h = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    info = {"host": host, "port": int(port), "pid": os.getpid(), "started_at": now(),
            "url": f"{'https' if tls else 'http'}://{'[' + h + ']' if ':' in h else h}:{int(port)}"}
    try:
        _home().mkdir(parents=True, exist_ok=True)
        (_home() / "serve.json").write_text(json.dumps(info))
    except OSError:
        pass
    return info


def serve_info() -> dict | None:
    """The live `commons serve` HTTP bind, or None when nothing (still) runs."""
    try:
        d = json.loads((_home() / "serve.json").read_text())
        os.kill(int(d["pid"]), 0)
    except PermissionError:  # alive, owned by another user
        return d
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return d


def health(s: Store, timeout: float = 5) -> dict:
    st = state(s)
    if not st.get("url"):
        sv = serve_info()
        if sv:
            from .dashboard import _private_host  # no tunnel, but the hub listens on HTTP (LAN / tailnet bind)
            return {"provider": "http", "url": sv["url"], "public": not _private_host(sv["host"]),
                    "health": probe(sv["url"], timeout)}
        return st | {"health": None}
    alive = True
    if st.get("pid"):
        try:
            os.kill(st["pid"], 0)
        except OSError:
            alive = False
    h = probe(st["url"], timeout) | {"process_alive": alive, "checked_at": now()}
    st["health"] = h
    _save(s, st)
    return st


# ------------------------------------------------------------------ background tunnel processes
def _spawn(cmd: list[str], url_rx: str, timeout: float = 30, log: Path | None = None) -> tuple[int, str]:
    """Start a long-running tunnel client; read its output until the public URL appears."""
    exe = _which(cmd[0])
    if not exe:
        raise CommonsError(f"`{cmd[0]}` not found on PATH; install it first (see docs/networking.md)")
    log = log or (_home() / f"expose-{cmd[0]}.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.Popen([exe, *cmd[1:]], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         text=True, start_new_session=True)
    rx = re.compile(url_rx)
    deadline = time.time() + timeout
    lines = []
    with open(log, "a") as lf:
        while time.time() < deadline:
            line = p.stdout.readline()
            if not line:
                if p.poll() is not None:
                    break
                continue
            lf.write(line)
            lines.append(line)
            m = rx.search(line)
            if m:
                _drain(p, log)
                return p.pid, m.group(1)
    p.kill()
    raise CommonsError(f"{cmd[0]} did not report a URL within {timeout}s: {''.join(lines)[-400:]}")


def _drain(p: subprocess.Popen, log: Path) -> None:
    import threading

    def run():
        with open(log, "a") as lf:
            for line in p.stdout:
                lf.write(line)
    threading.Thread(target=run, daemon=True).start()


def _stop_pid(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


# ------------------------------------------------------------------ providers
def up(s: Store, provider: str, *, port: int = 8765, host: str = "127.0.0.1", public: bool = False,
       name: str | None = None, hostname: str | None = None, token_file: str | None = None, ssh_target: str | None = None,
       oauth: str | None = None, oauth_allow: list[str] | None = None, cidr_allow: list[str] | None = None,
       domain: str | None = None, https_port: int = 443, allow_plain_http: bool = False, iface: str = "wg0",
       proxy: str = "caddy") -> dict:
    if provider not in PROVIDERS:
        raise CommonsError(f"provider must be one of {', '.join(PROVIDERS)}")
    target = f"http://{host}:{port}"
    is_public = provider in PUBLIC or (provider == "tailscale" and public)
    warnings = []
    if provider == "proxy":
        return {"provider": "proxy", "config": proxy_config(proxy, domain or "commons.example.com", port, host),
                "note": "save as Caddyfile (caddy run) or an nginx site; Caddy gets certificates automatically"}
    if provider in ("local", "zerotier", "wireguard") or (provider == "tailscale" and not public and https_port == 0):
        h = bind_host(provider, iface) if provider != "local" else "127.0.0.1"
        st = {"provider": provider, "url": f"http://{h}:{port}", "public": False, "bind": h, "started_at": now(),
              "note": f"run `commons serve --transport http --host {h} --port {port}`"}
        return _save(s, st)
    pr = require_auth(target, is_public)
    old = state(s)
    if old.get("pid"):
        _stop_pid(old["pid"])
    if provider == "tailscale":
        info = tailscale_info()
        cmd = ["tailscale", "funnel" if public else "serve", "--bg", f"--https={https_port}", target]
        _out(cmd, timeout=60)
        url = f"https://{info['dns']}" + ("" if https_port == 443 else f":{https_port}")
        if public:
            warnings.append("Tailscale Funnel makes this reachable from the public internet; every request needs a key.")
        st = {"provider": "tailscale", "mode": "funnel" if public else "serve", "url": url, "public": public, "pid": None,
              "stop": ["tailscale", "funnel" if public else "serve", f"--https={https_port}", "off"]}
    elif provider == "cloudflare":
        if name or token_file:
            cmd = ["cloudflared", "tunnel", "--no-autoupdate", "run"]
            if token_file:
                cmd += ["--token-file", token_file]  # keeps the token out of argv / ps
            cmd += ["--url", target] + ([name] if name else [])
            if not hostname:
                raise CommonsError("named tunnels need --hostname (the public DNS name routed to the tunnel)")
            pid, _ = _spawn(cmd, r"(Registered tunnel connection|Connection [0-9a-f-]+ registered)", 45)
            url = f"https://{hostname}"
        else:
            pid, url = _spawn(["cloudflared", "tunnel", "--no-autoupdate", "--url", target],
                              r"(https://[a-z0-9-]+\.trycloudflare\.com)", 45)
            warnings.append("Quick tunnels get a random public URL with no uptime guarantee; use a named tunnel + "
                            "Cloudflare Access for anything long-lived.")
        if os.environ.get("COMMONS_CF_ACCESS_AUD"):
            warnings.append("Cloudflare Access JWTs are verified by Commons (COMMONS_CF_ACCESS_TEAM/AUD).")
        st = {"provider": "cloudflare", "mode": "named" if name or token_file else "quick", "url": url, "public": True, "pid": pid}
    elif provider == "ngrok":
        flags = []
        if oauth:
            flags += [f"--oauth={oauth}"] + [f"--oauth-allow-email={e}" for e in oauth_allow or []]
        flags += [f"--cidr-allow={c}" for c in cidr_allow or []]
        if domain:
            flags += [f"--url={domain}"]
        if _which("ngrok"):
            pid, url = _spawn(["ngrok", "http", target, "--log=stdout", "--log-format=json", *flags],
                              r'"url":"(https://[^"]+)"', 45)
        else:
            pid, url = _ngrok_sdk(port, host, oauth, oauth_allow, cidr_allow, domain)
        st = {"provider": "ngrok", "url": url, "public": True, "pid": pid,
              "restrictions": {"oauth": oauth, "allow": oauth_allow, "cidr": cidr_allow}}
    elif provider == "ssh":
        dest = ssh_target or "nokey@localhost.run"
        pid, url = _spawn(["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30",
                           "-o", "ExitOnForwardFailure=yes", "-R", f"80:{host}:{port}", dest],
                          r"(https://[a-z0-9.-]+\.(?:lhr\.life|localhost\.run|serveo\.net|[a-z0-9.-]+))", 45)
        st = {"provider": "ssh", "url": url, "public": True, "pid": pid, "via": dest}
    elif provider == "bore":
        if not allow_plain_http:
            raise CommonsError("bore forwards plain TCP: keys would cross the internet unencrypted. "
                               "Pass --allow-plain-http to accept that, or use tailscale/cloudflare/ngrok.")
        to = domain or "bore.pub"
        pid, hp = _spawn(["bore", "local", str(port), "--local-host", host, "--to", to], r"listening at ([\w.-]+:\d+)", 30)
        st = {"provider": "bore", "url": f"http://{hp}", "public": True, "pid": pid}
        warnings.append("plain HTTP over the internet: rotate keys after use")
    else:
        raise CommonsError(provider)
    st |= {"target": target, "started_at": now(), "warnings": warnings, "auth_probe": pr}
    _save(s, st)
    st["snippets"] = write_snippets(s)
    return st


def _ngrok_sdk(port, host, oauth, oauth_allow, cidr_allow, domain):
    try:
        import ngrok  # type: ignore
    except ImportError as e:
        raise CommonsError("neither the ngrok CLI nor the `ngrok` Python SDK is installed (pip install ngrok)") from e
    opts = {"authtoken_from_env": True}
    if oauth:
        opts |= {"oauth_provider": oauth, "oauth_allow_emails": oauth_allow or []}
    if cidr_allow:
        opts["ip_restriction_allow_cidrs"] = cidr_allow
    if domain:
        opts["domain"] = domain
    lst = ngrok.forward(f"{host}:{port}", **opts)
    return os.getpid(), lst.url()  # lives in this process: keep `commons expose` running


def down(s: Store) -> dict:
    st = state(s)
    if st.get("stop"):
        try:
            _out(st["stop"], timeout=30)
        except CommonsError as e:
            st.setdefault("warnings", []).append(str(e))
    _stop_pid(st.get("pid") if st.get("pid") != os.getpid() else None)
    new = {"provider": "local", "url": None, "public": False, "stopped": st.get("provider"), "stopped_at": now()}
    _save(s, new)
    write_snippets(s)
    return new


# ------------------------------------------------------------------ connection snippets
def snippets(url: str, key_placeholder: str = "${COMMONS_API_KEY}") -> dict:
    mcp = url.rstrip("/") + "/mcp"
    auth = f"Bearer {key_placeholder}"
    return {
        "claude-code.sh": f'claude mcp add --transport http commons {mcp} --header "Authorization: {auth}"\n',
        "cursor-mcp.json": json.dumps({"mcpServers": {"commons": {"url": mcp, "headers": {"Authorization": auth}}}}, indent=2),
        "claude-desktop.json": json.dumps({"mcpServers": {"commons": {"command": "npx", "args": [
            "-y", "mcp-remote", mcp, "--header", f"Authorization:{auth}"]}}}, indent=2),
        "codex-config.toml": f'[mcp_servers.commons]\nurl = "{mcp}"\nbearer_token_env_var = "COMMONS_API_KEY"\n',
        "gemini-settings.json": json.dumps({"mcpServers": {"commons": {"httpUrl": mcp, "headers": {"Authorization": auth}}}}, indent=2),
        "worker.sh": f"commons worker --url {mcp} --api-key {key_placeholder} --preset claude\n",
        "a2a.txt": f"{url.rstrip('/')}/.well-known/agent-card.json\n",
    }


def write_snippets(s: Store, url: str | None = None) -> str:
    """(Re)write ~/.commons/connect/* with the currently reachable URL; keys stay placeholders."""
    url = url or reachable_url(s)
    d = _home() / "connect"
    d.mkdir(parents=True, exist_ok=True)
    for name, body in snippets(url).items():
        (d / name).write_text(body)
    (d / "README.txt").write_text(f"Commons is reachable at {url}\nSet COMMONS_API_KEY to the key from "
                                  f"`commons register-agent NAME` (or `commons connect`). Regenerated {time.ctime()}.\n")
    return str(d)


# ------------------------------------------------------------------ guidance generators
def proxy_config(kind: str, domain: str, port: int, host: str = "127.0.0.1") -> str:
    if kind == "nginx":
        return f"""server {{
    listen 443 ssl http2;
    server_name {domain};
    ssl_certificate     /etc/letsencrypt/live/{domain}/fullchain.pem;   # certbot --nginx -d {domain}
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;
    location / {{
        proxy_pass http://{host}:{port};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;            # SSE / streamable HTTP
        proxy_read_timeout 3600s;
    }}
}}
"""
    return f"""{domain} {{
    # Caddy obtains and renews the TLS certificate automatically.
    reverse_proxy {host}:{port} {{
        flush_interval -1   # stream SSE / MCP responses
    }}
}}
"""


def service_unit(kind: str, port: int = 8765, host: str = "127.0.0.1", db: str | None = None, exe: str | None = None,
                 args: list[str] | None = None, name: str = "commons", description: str = "Commons memory hub (MCP over HTTP)",
                 harden: bool = True, env: dict | None = None) -> str:
    """A service definition for `commons serve` (default) or any other commons command (`args`, e.g. the agent
    daemon, which runs an agent CLI and therefore must not be sandboxed: harden=False)."""
    exe = exe or shutil.which("commons") or f"{sys.executable} -m commons_memory"
    db = db or str(_home() / "commons.db")
    argv = args if args is not None else ["serve", "--transport", "http", "--host", host, "--port", str(port)]
    env = {"COMMONS_DB": db, "COMMONS_REQUIRE_KEYS": "1"} | (env or {})
    label = "dev.commons.server" if name == "commons" else f"dev.commons.{name}"
    if kind == "systemd":
        hard = f"NoNewPrivileges=true\nProtectSystem=strict\nReadWritePaths={Path(db).parent}\n" if harden else ""
        envs = "".join(f"Environment={k}={v}\n" for k, v in env.items())
        return f"""[Unit]
Description={description}
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
{envs}ExecStart={exe} {" ".join(shlex.quote(a) for a in argv)}
Restart=on-failure
RestartSec=3
{hard}
[Install]
WantedBy=default.target
# install: save as ~/.config/systemd/user/{name}.service ; systemctl --user enable --now {name}
"""
    if kind == "launchd":
        parts = "".join(f"<string>{html.escape(a)}</string>" for a in [*exe.split(), *argv])
        envs = "".join(f"<key>{k}</key><string>{html.escape(str(v))}</string>" for k, v in env.items())
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>{parts}</array>
  <key>EnvironmentVariables</key><dict>{envs}</dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>{Path(db).parent}/{name}.log</string>
</dict></plist>
<!-- install: save as ~/Library/LaunchAgents/{label}.plist ; launchctl load -w that file -->
"""
    args = " ".join(argv)
    if kind == "windows":
        return f"""REM Windows: run Commons as a service with NSSM (https://nssm.cc) or WinSW.
nssm install Commons "{exe}" {args}
nssm set Commons AppEnvironmentExtra COMMONS_DB={db} COMMONS_REQUIRE_KEYS=1
nssm set Commons Start SERVICE_AUTO_START
nssm start Commons
REM Alternative without extra tools (runs at logon):
REM schtasks /Create /SC ONLOGON /TN Commons /TR "\\"{exe}\\" {args}"
"""
    raise CommonsError("kind must be systemd|launchd|windows")


# ------------------------------------------------------------------ Cloudflare Access JWT validation
class CloudflareAccess:
    """Verifies `Cf-Access-Jwt-Assertion` (RS256, team JWKS, audience tag). Enabled when COMMONS_CF_ACCESS_TEAM and
    COMMONS_CF_ACCESS_AUD are set. Service tokens arrive as JWTs with a `common_name` claim instead of `email`."""

    def __init__(self, team: str | None = None, aud: str | None = None, certs_url: str | None = None):
        self.team = team or os.environ.get("COMMONS_CF_ACCESS_TEAM")
        self.aud = aud or os.environ.get("COMMONS_CF_ACCESS_AUD")
        t = (self.team or "").removeprefix("https://").split(".")[0]
        self.issuer = f"https://{t}.cloudflareaccess.com" if t else None
        self.certs_url = certs_url or os.environ.get("COMMONS_CF_ACCESS_CERTS") or (f"{self.issuer}/cdn-cgi/access/certs" if t else None)
        self._jwks = None

    @property
    def enabled(self) -> bool:
        return bool(self.team and self.aud)

    def verify(self, token: str) -> dict | None:
        import jwt
        if not token:
            return None
        if self._jwks is None:
            self._jwks = jwt.PyJWKClient(self.certs_url, cache_keys=True, lifespan=3600)
        try:
            key = self._jwks.get_signing_key_from_jwt(token).key
            return jwt.decode(token, key, algorithms=["RS256"], audience=self.aud,
                              issuer=None if os.environ.get("COMMONS_CF_ACCESS_CERTS") else self.issuer)
        except Exception:  # noqa: BLE001 - any failure means "not verified"
            return None

    @staticmethod
    def identity(claims: dict) -> str:
        return claims.get("email") or claims.get("common_name") or claims.get("sub") or "unknown"
