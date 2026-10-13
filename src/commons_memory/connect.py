"""v0.8.1 'Connect an agent': copy-ready setup for any AI (dashboard page, `commons connect <platform>`, JSON API).

Everything here is pure string generation except `create_key` (wraps Store.register_agent) and `status`.
Keys are only ever embedded in output when the caller passes one (i.e. right after creating it); otherwise a
placeholder is used.
"""
from __future__ import annotations

import ipaddress
import json
import os
import shlex

from . import __version__
from .ids import iso, now
from .store import CommonsError, Store

KEY_PLACEHOLDER = "<YOUR_COMMONS_KEY>"
ROLES = ("read", "write", "admin")

# id -> (label, skill platform for `commons install-skill`, short hint)
PLATFORMS: dict[str, tuple[str, str, str]] = {
    "claude-code": ("Claude Code", "claude-code", "Adds an HTTP MCP server to Claude Code."),
    "codex": ("Codex CLI", "codex", "Adds Commons to ~/.codex/config.toml (key read from an env var)."),
    "cursor": ("Cursor", "cursor", "Merge into ~/.cursor/mcp.json (or .cursor/mcp.json in the project)."),
    "gemini": ("Gemini CLI", "generic", "Merge into ~/.gemini/settings.json."),
    "opencode": ("OpenCode", "generic", "Adds a remote MCP server to ~/.config/opencode/opencode.json."),
    "copilot": ("GitHub Copilot", "generic", "Copilot CLI / agent mode: ~/.copilot/mcp-config.json."),
    "windsurf": ("Windsurf", "generic", "~/.codeium/windsurf/mcp_config.json and global_rules.md."),
    "zed": ("Zed", "generic", "context_servers in ~/.config/zed/settings.json, rules in ~/.config/zed/AGENTS.md."),
    "cline": ("Cline", "generic", "Cline's MCP settings and a rule file in ~/Documents/Cline/Rules."),
    "roo": ("Roo Code", "generic", "Roo's MCP settings and a rule file in ~/.roo/rules."),
    "kilo": ("Kilo Code", "generic", "~/.config/kilo/kilo.json and ~/.config/kilo/AGENTS.md."),
    "amp": ("Amp", "generic", "amp.mcpServers in ~/.config/amp/settings.json."),
    "factory": ("Factory Droid", "generic", "~/.factory/mcp.json."),
    "warp": ("Warp", "generic", "~/.warp/.mcp.json."),
    "qwen": ("Qwen Code", "generic", "~/.qwen/settings.json."),
    "crush": ("Crush", "generic", "~/.config/crush/crush.json and CRUSH.md."),
    "goose": ("Goose", "generic", "an extension block for ~/.config/goose/config.yaml (printed, not edited)."),
    "chatgpt": ("ChatGPT / other MCP HTTP clients", "chatgpt",
                "Any client that speaks streamable HTTP MCP: point it at the URL and send the key as a Bearer header."),
    "any": ("Any AI (prompt only)", "generic", "Paste the prompt; MCP-less AIs can use the plain HTTP agent API."),
    "docker": ("Docker / self-host", "generic", "Run your own Commons server, then connect agents to it."),
}
ALIASES = {"open-code": "opencode", "claude": "claude-code", "codex-cli": "codex", "gemini-cli": "gemini", "openai": "chatgpt",
           "http": "chatgpt", "generic": "any", "self-host": "docker", "selfhost": "docker"}


def norm_platform(p: str) -> str:
    p = (p or "").strip().lower()
    p = ALIASES.get(p, p)
    if p not in PLATFORMS:
        raise CommonsError(f"unknown platform {p!r}; choose one of: {', '.join(PLATFORMS)}")
    return p


# ------------------------------------------------------------------ URL detection
def _wild(h: str) -> bool:
    try:
        return not h or ipaddress.ip_address(h.strip("[]")).is_unspecified
    except ValueError:
        return False


def _loop(h: str) -> bool:
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h.strip("[]")).is_loopback
    except ValueError:
        return False


def _split_host(host_header: str) -> str:
    h = (host_header or "").strip()
    if h.startswith("["):
        return h[: h.find("]") + 1]
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def mcp_bind() -> tuple[str, int, bool]:
    """The MCP server's bind (set by `commons serve`; overridable via env for split deployments)."""
    host = os.environ.get("COMMONS_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("COMMONS_MCP_PORT", "8765"))
    return host, port, bool(os.environ.get("COMMONS_TLS_CERT"))


def detect_url(request_host: str = "", bind_host: str | None = None, port: int | None = None,
               tls: bool | None = None) -> str:
    """Best guess at the MCP URL a *client* can reach.
    COMMONS_PUBLIC_URL wins. A wildcard or loopback bind is reached via the host the browser used; a specific
    non-loopback bind is reached at that address."""
    pub = os.environ.get("COMMONS_PUBLIC_URL")
    if pub:
        return pub.rstrip("/").removesuffix("/mcp") + "/mcp"
    b_host, b_port, b_tls = mcp_bind()
    bind_host = b_host if bind_host is None else bind_host
    port = b_port if port is None else port
    tls = b_tls if tls is None else tls
    req = _split_host(request_host)
    if _wild(bind_host):
        host = req or "127.0.0.1"
    elif _loop(bind_host):
        host = req if req and _loop(req) else "127.0.0.1"
    else:
        host = f"[{bind_host}]" if ":" in bind_host and not bind_host.startswith("[") else bind_host
    return f"{'https' if tls else 'http'}://{host}:{port}/mcp"


# ------------------------------------------------------------------ text generators
def prompt_text(url: str, agent: str, key: str | None = None, project: str = "") -> str:
    """The same master instructions `commons instructions` installs, so the prompt can never go stale."""
    from .instructions import master
    text = master(url, agent)
    if project:
        text += f"\nProject: {project}. Pass project=\"{project}\" to tools that take one."
    base = url.rstrip("/").removesuffix("/mcp")
    return text + (f"\nNo MCP? Use HTTP with the same key: GET {base}/agent/recv and POST {base}/agent/send.")


def verify_command(url: str, key: str | None = None) -> str:
    """One line (bash + curl): initialize, then call the `whoami` tool; prints OK or FAIL."""
    k = key or KEY_PLACEHOLDER
    h = "-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream'"
    init = ('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26",'
            '"capabilities":{},"clientInfo":{"name":"commons-verify","version":"1"}}}')
    call = '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"whoami","arguments":{}}}'
    note = '{"jsonrpc":"2.0","method":"notifications/initialized"}'
    return (f"U={shlex.quote(url)}; K={shlex.quote(k)}; "
            f"S=$(curl -sS -m 10 -D - -o /dev/null -X POST \"$U\" -H \"Authorization: Bearer $K\" {h} -d '{init}' "
            "| tr -d '\\r' | awk -F': ' 'tolower($1)==\"mcp-session-id\"{print $2}'); "
            f"curl -sS -m 10 -o /dev/null -X POST \"$U\" -H \"Authorization: Bearer $K\" -H \"Mcp-Session-Id: $S\" {h} -d '{note}'; "
            f"curl -sS -m 10 -X POST \"$U\" -H \"Authorization: Bearer $K\" -H \"Mcp-Session-Id: $S\" {h} -d '{call}' "
            "| grep -q commons-ok && echo OK || echo FAIL")


def config_block(platform: str, url: str, key: str | None = None, name: str = "commons") -> dict:
    k = key or KEY_PLACEHOLDER
    auth = f"Bearer {k}"
    if platform == "claude-code":
        return {"title": "Install command", "lang": "bash",
                "text": f"claude mcp add --transport http --scope user commons {url} --header {shlex.quote('Authorization: ' + auth)}"}
    if platform == "codex":
        return {"title": "~/.codex/config.toml (then export COMMONS_API_KEY in your shell profile)", "lang": "toml",
                "text": f'export COMMONS_API_KEY={shlex.quote(k)}\n\n# ~/.codex/config.toml\n[mcp_servers.commons]\nurl = "{url}"\n'
                        'bearer_token_env_var = "COMMONS_API_KEY"'}
    if platform == "cursor":
        return {"title": "~/.cursor/mcp.json", "lang": "json",
                "text": json.dumps({"mcpServers": {"commons": {"url": url, "headers": {"Authorization": auth}}}}, indent=2)}
    if platform == "gemini":
        return {"title": "~/.gemini/settings.json", "lang": "json",
                "text": json.dumps({"mcpServers": {"commons": {"httpUrl": url, "headers": {"Authorization": auth}}}}, indent=2)}
    if platform == "opencode":
        return {"title": "~/.config/opencode/opencode.json", "lang": "json",
                "text": json.dumps({"$schema": "https://opencode.ai/config.json", "mcp": {"commons": {
                    "type": "remote", "url": url, "enabled": True, "headers": {"Authorization": auth}}}}, indent=2)}
    if platform == "chatgpt":
        return {"title": "MCP server settings (streamable HTTP)", "lang": "text",
                "text": f"Server URL: {url}\nTransport: Streamable HTTP\nAuthentication: header  Authorization: {auth}\n"
                        "(ChatGPT connectors need an HTTPS URL reachable from the internet: run `commons expose cloudflare` "
                        "first and use that URL.)\n\nGeneric JSON:\n"
                        + json.dumps({"mcpServers": {"commons": {"type": "http", "url": url,
                                                                  "headers": {"Authorization": auth}}}}, indent=2)}
    if platform == "any":
        base = url.removesuffix("/mcp")
        return {"title": "No-MCP fallback (plain HTTP)", "lang": "bash",
                "text": f"curl -s {base}/agent/recv -H 'Authorization: Bearer {k}'\n"
                        f"curl -s -X POST {base}/agent/send -H 'Authorization: Bearer {k}' -H 'Content-Type: application/json' "
                        "-d '{\"to\":\"*\",\"text\":\"hello from my AI\"}'"}
    if platform == "docker":
        return {"title": "Run Commons (Docker)", "lang": "bash",
                "text": "docker run -d --name commons --restart unless-stopped -p 8765:8765 -p 8766:8766 \\\n"
                        "  -v commons-data:/data ghcr.io/neoo-blue/commons-memory:latest\n"
                        "# then create a key inside the container:\n"
                        f"docker exec commons commons connect any --name {shlex.quote(name)} --new-key"}
    raise CommonsError(platform)


def skill_command(platform: str, agent: str) -> str:
    sp = PLATFORMS[platform][1]
    return f"pip install commons-memory && commons install-skill {sp} --agent {shlex.quote(agent)}"


TROUBLESHOOTING = [
    {"symptom": "401 Unauthorized", "fix": "The key is missing or wrong. Check the client sends exactly "
     "`Authorization: Bearer <key>` (no quotes, no extra spaces). Lost it? Create a new key here; the old one stops working."},
    {"symptom": "Unreachable / timeout / connection refused", "fix": "Network, not Commons. From the client machine "
     "run `curl -s -o /dev/null -w '%{http_code}' <URL>` (expect 401 without a key). On Tailscale, LAN addresses need a "
     "subnet route advertised by a node on that LAN and `tailscale up --accept-routes` on the client (Linux defaults to off). "
     "Check the host firewall allows the port, and that the URL uses an IP the client can reach (not 127.0.0.1 from another machine)."},
    {"symptom": "Tool missing / schema mismatch / 'unknown tool'", "fix": "The client cached an older tool list. Fully "
     "restart the client (or toggle the server off/on in its MCP settings) after upgrading Commons."},
    {"symptom": "Connected but never picks up work / says it will check every few minutes", "fix": "A chat window can't "
     "run while idle, so it only sees work when you talk to it. For instant pickup run the agent daemon on the agent's "
     "machine (the instant-wake step of the setup command): it holds an open connection and starts the agent CLI within a second of being "
     "assigned work or summoned. Or register a wake channel so Commons pings it: `commons connect <name> --wake "
     "ntfy:topic=...` (also webhook, slack, telegram, email, command)."},
    {"symptom": "Last seen never updates", "fix": "Run the verification command: OK means the server saw the key. If it "
     "prints FAIL with a 401 above, see the first item; if curl cannot connect, see the second."},
]


def bundle(platform: str, url: str, agent: str = "my-agent", key: str | None = None, project: str = "") -> dict:
    platform = norm_platform(platform)
    url = url.strip()
    if not url.rstrip("/").endswith("/mcp"):
        url = url.rstrip("/") + "/mcp"
    label, _, hint = PLATFORMS[platform]
    from .setup import CLIENTS, LABELS
    prompt = {"id": "prompt", "lang": "text", "text": prompt_text(url, agent, key, project)}
    if platform in CLIENTS:  # one command does everything; each step can be unticked
        base = (f"pip install -U commons-memory && commons setup --client {platform} --agent {shlex.quote(agent)} "
                f"--url {url.removesuffix('/mcp')}")
        blocks = [{"id": "setup", "kind": "setup", "title": "Set up this AI: one command (asks for the key once)",
                   "lang": "bash", "base": base, "text": base,
                   "steps": [{"id": k, "label": v} for k, v in LABELS.items()]},
                  prompt | {"title": "Chat app with no terminal (claude.ai, ChatGPT, Grok, Muse)? Paste this prompt "
                                     "into its custom instructions instead", "collapsed": True}]
    else:
        blocks = [config_block(platform, url, key, agent) | {"id": "config"},
                  prompt | {"title": "Prompt: paste into the AI's custom instructions or system prompt"},
                  {"id": "verify", "title": "Verify (prints OK)", "lang": "bash", "text": verify_command(url, key)}]
    return {"platform": platform, "label": label, "hint": hint, "url": url, "agent": agent, "project": project,
            "key_included": bool(key), "blocks": blocks, "troubleshooting": TROUBLESHOOTING,
            "platforms": {k: v[0] for k, v in PLATFORMS.items()}, "version": __version__}


WAKE_CLI = {"claude-code": "claude", "codex": "codex", "gemini": "gemini", "cursor": "cursor-agent", "opencode": "opencode"}


def wake_block(platform: str, url: str, agent: str) -> dict:
    """v0.9.3 instant wake setup for one platform: woken within ~1 s of a message, assignment, lease, debate turn or
    board @mention, instead of polling. Works on plain LAN/tailnet HTTP; the client dials out, nothing is exposed."""
    base = url.rstrip("/").removesuffix("/mcp")
    key = f"~/.commons/agents/{agent}.key"
    title = "Instant wake (recommended): wakes this agent within a second of new work, no polling"
    if platform in WAKE_CLI:
        cli = WAKE_CLI[platform]
        text = (f"# save the agent key once:  install -m600 /dev/stdin {key}  (paste key, Ctrl-D)\n"
                f"# A) interactive session: run {cli} inside a PTY that Commons types wake prompts into\n"
                f"commons wake daemon --agent {agent} --url {base} --pty '{cli}'\n"
                f"# B) already running in tmux: inject into that pane\n"
                f"commons wake daemon --agent {agent} --url {base} --tmux {agent}:0\n"
                f"# C) headless: start a fresh run per wake (events JSON on stdin)\n"
                f"commons wake daemon --agent {agent} --url {base} --exec '{cli} -p \"$COMMONS_WAKE_PROMPT\"'\n"
                f"# keep C running as a service: commons agent daemon --agent {agent} --preset "
                f"{ {'cursor-agent': 'cursor'}.get(cli, cli)} --url {base} --install\n"
                "# presence then shows wake_mode=instant; stop polling loops / 'check every 5 minutes' crons")
    elif platform == "chatgpt":
        text = (f"# an always-on HTTP agent: hold the SSE stream (reconnect with Last-Event-ID; missed events replay)\n"
                f"curl -N -H \"Authorization: Bearer $(cat {key})\" {base}/agent/stream\n"
                f"# ack what you handled:  curl -X POST -H 'Authorization: Bearer ...' -d '{{\"event_id\":\"wev_...\"}}' "
                f"{base}/agent/wake/ack\n"
                f"# or WebSocket {base.replace('http', 'ws', 1)}/agent/ws, or a signed webhook with retries: "
                f"commons connect {agent} --wake webhook:url=https://your-host/hook")
    elif platform == "docker":
        text = (f"# on the agent's machine (any OS with Python 3.10+):\npip install commons-memory\n"
                f"commons wake daemon --agent {agent} --url {base} --exec './on-wake.sh'")
    else:
        text = (f"# chat-only AIs can't run while idle. Wake them by push instead:\n"
                f"commons connect {agent} --wake ntfy:topic={agent}-wake   # phone/desktop push\n"
                f"# or run any script on wake from an always-on machine:\n"
                f"commons wake daemon --agent {agent} --url {base} --exec 'your-command'")
    return {"id": "wake", "title": title, "lang": "bash", "text": text}


def render_text(b: dict) -> str:
    out = [f"# Connect {b['label']} to Commons", f"URL: {b['url']}   agent: {b['agent']}", b["hint"], ""]
    for blk in b["blocks"]:
        out += [f"## {blk['title']}", blk["text"].rstrip(), ""]
    out.append("## Troubleshooting")
    out += [f"- {t['symptom']}: {t['fix']}" for t in b["troubleshooting"]]
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ keys + presence
def key_creation_allowed(bind_host: str, client_ip: str, has_auth: bool, role: str = "admin") -> tuple[bool, str]:
    """Same spirit as dashboard._private_host: an authed (admin) dashboard may mint keys from anywhere; a tokenless
    dashboard only when it is bound to a private address and the browser is on a private address too."""
    from .dashboard import _private_host
    if has_auth:
        return (role == "admin", "" if role == "admin" else "read-only dashboard user")
    if not _private_host(bind_host):
        return False, "tokenless dashboard on a non-private bind: set COMMONS_DASHBOARD_TOKEN to create keys"
    if client_ip and not _private_host(client_ip):
        return False, "key creation from a public address needs a dashboard token"
    return True, ""


def create_key(store: Store, name: str, role: str = "write", project: str = "", platform: str = "") -> dict:
    name = (name or "").strip()
    if not name or len(name) > 64 or any(c.isspace() for c in name):
        raise CommonsError("agent name required (no spaces, max 64 chars)")
    if role not in ROLES:
        raise CommonsError(f"role must be one of {', '.join(ROLES)}")
    reg = store.register_agent(name, vendor=platform or "", capabilities=[f"role:{role}"])
    if project:
        from .auth import ACL
        ACL(store).grant(project, name, role)
    store.x("UPDATE agents SET last_seen=0 WHERE id=?", (reg["id"],))  # 0 = never, until the agent itself connects
    store.audit(reg["id"], "agent.key_created", reg["id"], {"role": role, "via": "connect"})
    return {"id": reg["id"], "name": name, "role": role, "api_key": reg["api_key"], "created_at": iso(now()),
            "note": "Shown once. Only a hash is stored."}


def status(store: Store, name: str) -> dict:
    a = store.get_agent(name) if name else None
    if not a:
        return {"name": name, "exists": False}
    ls = a.get("last_seen")
    return {"name": a["name"], "id": a["id"], "exists": True, "last_seen": iso(ls) if ls else None,
            "seconds_ago": round(now() - ls) if ls else None, "connected": bool(ls and now() - ls < 300)}
