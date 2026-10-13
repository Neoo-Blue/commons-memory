"""`commons setup`: one command that connects an AI to Commons completely. Every step is recommended and on by default;
--no-<step> leaves one out. Steps: save the key (0600, asked once, never on the command line), register the MCP server
in the AI, install the master instructions (and skill), install auto-capture hooks, install the instant-wake service,
hand over existing memories, then verify the connection.
"""
from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

STEPS = ["mcp", "instructions", "hooks", "wake", "import", "verify"]
LABELS = {"mcp": "connect it to Commons (MCP)", "instructions": "teach it the Commons routine (always-loaded instructions + skill)",
          "hooks": "auto-capture every message and tool call", "wake": "instant wake when work arrives",
          "import": "hand over its existing memories", "verify": "verify the connection"}
# client -> instant-wake preset (None: the client has no headless mode to wake)
CLIENTS = {"claude-code": "claude", "codex": "codex", "gemini": "gemini", "cursor": "cursor", "opencode": "opencode",
           "copilot": "copilot", "cline": "cline", "kilo": "kilo", "amp": "amp", "factory": "factory", "qwen": "qwen",
           "crush": "crush", "goose": "goose", "windsurf": None, "zed": None, "roo": None, "warp": None}


def _vscode_global(ext: str) -> str:
    import platform
    base = "~/Library/Application Support/Code/User" if platform.system() == "Darwin" else "~/.config/Code/User"
    return f"{base}/globalStorage/{ext}/settings"


# client -> (global MCP config file, section, entry) for remote streamable-HTTP servers, from each client's docs
JSON_MCP = {
    "cursor": ("~/.cursor/mcp.json", "mcpServers", lambda u, a: {"url": u, "headers": {"Authorization": a}}),
    "gemini": ("~/.gemini/settings.json", "mcpServers", lambda u, a: {"httpUrl": u, "headers": {"Authorization": a}}),
    "opencode": ("~/.config/opencode/opencode.json", "mcp",
                 lambda u, a: {"type": "remote", "url": u, "enabled": True, "headers": {"Authorization": a}}),
    "windsurf": ("~/.codeium/windsurf/mcp_config.json", "mcpServers",
                 lambda u, a: {"serverUrl": u, "headers": {"Authorization": a}}),
    "zed": ("~/.config/zed/settings.json", "context_servers", lambda u, a: {"url": u, "headers": {"Authorization": a}}),
    "copilot": ("~/.copilot/mcp-config.json", "servers",
                lambda u, a: {"type": "http", "url": u, "headers": {"Authorization": a}}),
    "cline": (_vscode_global("saoudrizwan.claude-dev") + "/cline_mcp_settings.json", "mcpServers",
              lambda u, a: {"type": "streamableHttp", "url": u, "headers": {"Authorization": a}}),
    "roo": (_vscode_global("rooveterinaryinc.roo-cline") + "/mcp_settings.json", "mcpServers",
            lambda u, a: {"type": "streamable-http", "url": u, "headers": {"Authorization": a}}),
    "kilo": ("~/.config/kilo/kilo.json", "mcp",
             lambda u, a: {"type": "remote", "url": u, "headers": {"Authorization": a}, "enabled": True}),
    "amp": ("~/.config/amp/settings.json", "amp.mcpServers", lambda u, a: {"url": u, "headers": {"Authorization": a}}),
    "factory": ("~/.factory/mcp.json", "mcpServers",
                lambda u, a: {"type": "http", "url": u, "headers": {"Authorization": a}}),
    "warp": ("~/.warp/.mcp.json", "mcpServers", lambda u, a: {"url": u, "headers": {"Authorization": a}}),
    "qwen": ("~/.qwen/settings.json", "mcpServers", lambda u, a: {"httpUrl": u, "headers": {"Authorization": a}}),
    "crush": ("~/.config/crush/crush.json", "mcp",
              lambda u, a: {"type": "http", "url": u, "headers": {"Authorization": a}}),
}


def _key(agent: str) -> tuple[str, Path]:
    p = Path(os.environ.get("COMMONS_HOME") or Path.home() / ".commons") / "agents" / f"{agent}.key"
    if p.exists() and p.read_text().strip():
        return p.read_text().strip(), p
    k = (sys.stdin.read() if not sys.stdin.isatty() else getpass.getpass(f"Commons key for {agent} (input hidden): ")).strip()
    if not k:
        raise SystemExit("no key given (paste the key from the dashboard's Connect page)")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(k + "\n")
    p.chmod(0o600)
    return k, p


def _merge_json(path: str, key: str, value: dict, section: str = "mcpServers") -> str:
    p = Path(os.path.expanduser(path))
    try:
        cfg = json.loads(p.read_text()) if p.exists() and p.read_text().strip() else {}
    except ValueError:  # JSONC with comments: don't risk mangling the user's file
        return (f"skipped: {p} has comments, so add this under \"{section}\" by hand: "
                + json.dumps({key: {**value, **({"headers": {"Authorization": "Bearer <your key>"}} if "headers" in value else {})}}))
    cfg.setdefault(section, {})[key] = value
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2) + "\n")
    return f"wrote {p}"


def mcp(client: str, url: str, key: str) -> str:
    auth = f"Bearer {key}"
    if client == "claude-code":
        if not shutil.which("claude"):
            return "skipped: claude CLI not on PATH"
        subprocess.run(["claude", "mcp", "remove", "commons", "-s", "user"], capture_output=True)
        r = subprocess.run(["claude", "mcp", "add", "--transport", "http", "--scope", "user", "commons", url,
                            "--header", f"Authorization: {auth}"], capture_output=True, text=True)
        return "added to Claude Code (user scope)" if r.returncode == 0 else f"failed: {(r.stderr or r.stdout)[-200:]}"
    if client in JSON_MCP:
        path, section, entry = JSON_MCP[client]
        return _merge_json(path, "commons", entry(url, auth), section)
    if client == "goose":  # YAML config: print the block rather than edit the user's file
        return ("add this to ~/.config/goose/config.yaml under extensions:\n  commons:\n    enabled: true\n"
                f"    type: streamable_http\n    uri: {url}\n    headers:\n      Authorization: \"Bearer <your key>\"")
    if client == "codex":
        p = Path.home() / ".codex" / "config.toml"
        text = p.read_text() if p.exists() else ""
        if "[mcp_servers.commons]" in text:
            return f"already in {p}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text.rstrip() + f'\n\n[mcp_servers.commons]\nurl = "{url}"\nhttp_headers = {{ Authorization = "{auth}" }}\n')
        return f"wrote {p}"
    return "skipped: add the MCP server in this AI's settings by hand"


def run(client: str, agent: str, url: str, skip: set[str]) -> int:
    if client not in CLIENTS:
        raise SystemExit(f"--client is one of {', '.join(CLIENTS)}")
    base = url.rstrip("/").removesuffix("/mcp")
    mcp_url = base + "/mcp"
    key, keyfile = _key(agent)
    commons = shutil.which("commons") or sys.argv[0]
    ok = True

    def step(name, fn):
        nonlocal ok
        if name in skip:
            print(f"- {LABELS[name]}: skipped (--no-{name})")
            return
        try:
            print(f"- {LABELS[name]}: {fn()}")
        except Exception as e:  # noqa: BLE001 - report and keep going
            ok = False
            print(f"- {LABELS[name]}: FAILED: {str(e).replace(key, '[key]')[:300]}")

    print(f"Setting up {client} as Commons agent '{agent}' ({mcp_url}); key saved at {keyfile}")
    step("mcp", lambda: mcp(client, mcp_url, key))

    def instructions():
        from . import instructions as I
        return ", ".join(f"{Path(r['file']).name} {r['status']}" for r in I.install(client, mcp_url, agent) if r.get("file"))
    step("instructions", instructions)

    def hooks():
        from .capture import HOOK_TARGETS, install_hooks
        if client not in HOOK_TARGETS:
            return "this AI has no hook API; its installed instructions tell it to call turn() every turn instead"
        cmd = f"env COMMONS_URL={base} COMMONS_AGENT={agent} {commons}"
        r = install_hooks(client, command=cmd)
        return f"{len(r['events'])} hook event(s) in {r['path']}"
    step("hooks", hooks)

    def wake():
        if not CLIENTS[client]:
            return "this AI has no headless mode to wake; it picks up work when you open it"
        r = subprocess.run([commons, "agent", "daemon", "--agent", agent, "--preset", CLIENTS[client], "--url", base,
                            "--api-key-file", str(keyfile), "--install"], capture_output=True, text=True)
        return (r.stdout.strip().splitlines() or ["installed"])[-1][:200] if r.returncode == 0 else f"failed: {r.stderr[-200:]}"
    step("wake", wake)

    def imp():
        r = subprocess.run([commons, "import", "all", "--url", mcp_url, "--api-key-file", str(keyfile)],
                           capture_output=True, text=True, timeout=1800)
        return (r.stdout.strip().splitlines() or ["done"])[-1][:200] if r.returncode == 0 else f"failed: {r.stderr[-200:]}"
    step("import", imp)

    def verify():
        from .worker import McpClient
        me = json.loads(McpClient(mcp_url, key)._call("whoami", {}))
        if "commons-ok" not in json.dumps(me):
            raise RuntimeError(f"unexpected reply {str(me)[:120]}")
        return f"OK, signed in as {me.get('agent') or me.get('name') or agent}"
    step("verify", verify)
    print("Done. Restart the AI so it loads Commons." if ok else "Finished with failures above; fix them and run again.")
    return 0 if ok else 1


def doctor(client: str, agent: str, url: str, repair: bool = False) -> int:
    """Check every part of this AI's Commons connection; --repair re-runs the setup steps that are broken."""
    import platform
    from . import __version__
    from . import instructions as I
    base = url.rstrip("/").removesuffix("/mcp")
    keyfile = Path(os.environ.get("COMMONS_HOME") or Path.home() / ".commons") / "agents" / f"{agent}.key"
    checks: list[tuple[str, bool, str, str]] = []  # (name, ok, detail, setup step that fixes it)

    def add(name, ok, detail="", fix=""):
        checks.append((name, bool(ok), detail, fix))

    env_key = os.environ.get("COMMONS_API_KEY", "")
    if env_key:
        add("key available", True, "from COMMONS_API_KEY (e.g. injected by a secret manager)")
    else:
        add("key saved (0600)", keyfile.exists() and oct(keyfile.stat().st_mode)[-3:] == "600", str(keyfile), "key")
    key = env_key or (keyfile.read_text().strip() if keyfile.exists() else "")
    try:
        import urllib.request
        urllib.request.urlopen(base + "/bus/health", timeout=5).read()
        hub = True
    except Exception as e:  # noqa: BLE001
        hub = False
        add("hub reachable", False, f"{base}: {e}", "")
    if hub:
        add("hub reachable", True, base)
        try:
            from .worker import McpClient
            me = json.loads(McpClient(base + "/mcp", key)._call("whoami", {})) if key else {}
            add("key accepted", me.get("status") == "commons-ok", f"signed in as {me.get('agent')}", "key")
        except Exception as e:  # noqa: BLE001
            add("key accepted", False, str(e)[:120], "key")
    cfg = {"claude-code": Path.home() / ".claude.json", "codex": Path.home() / ".codex" / "config.toml",
           "goose": Path.home() / ".config" / "goose" / "config.yaml",
           **{k: Path(os.path.expanduser(v[0])) for k, v in JSON_MCP.items()}}[client]
    add("MCP server registered", cfg.exists() and base in cfg.read_text(), str(cfg), "mcp")
    from .server import INSTRUCTIONS
    files = [Path(os.path.expanduser(p)) for p in I.TARGETS[client]]
    current = all(f.exists() and INSTRUCTIONS in f.read_text() for f in files)
    add("master instructions current", current, ", ".join(map(str, files)), "instructions")
    from .capture import HOOK_TARGETS
    if client in HOOK_TARGETS:
        hp = Path(os.path.expanduser(HOOK_TARGETS[client][0]))
        add("capture hooks installed", hp.exists() and "commons" in hp.read_text() and " hook " in hp.read_text(),
            str(hp), "hooks")
    name = f"commons-agent-{agent}"
    if not CLIENTS[client]:
        pass  # no headless mode, nothing to wake
    elif platform.system() == "Darwin":
        r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
        add("instant wake running", f"dev.commons.{name}" in r.stdout, f"launchd dev.commons.{name}", "wake")
    elif shutil.which("systemctl"):
        r = subprocess.run(["systemctl", "--user", "is-active", name], capture_output=True, text=True)
        add("instant wake running", r.stdout.strip() == "active", f"systemd --user {name}", "wake")
    print(f"commons doctor: {client} as '{agent}' against {base} (client {__version__})")
    for n, ok, detail, _fix in checks:
        print(f"  {'OK  ' if ok else 'FAIL'} {n}" + (f"  ({detail})" if detail else ""))
    broken = {fix for _n, ok, _d, fix in checks if not ok and fix and fix != "key"}
    if any(not ok and fix == "key" for _n, ok, _d, fix in checks):
        print("  fix the key first: `commons setup` asks for it again (delete the key file if it is wrong)")
    if broken and repair:
        print(f"repairing: {', '.join(sorted(broken))}")
        return run(client, agent, url, set(STEPS) - broken - {"verify"})
    if broken:
        print(f"run again with --repair to fix: {', '.join(sorted(broken))}")
    return 0 if all(ok for _n, ok, _d, _f in checks) else 1
