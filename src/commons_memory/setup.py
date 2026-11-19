"""`commons setup`: one command that connects an AI to Commons completely. Every step is recommended and on by default;
--no-<step> leaves one out. Steps: save the key (0600, asked once, never on the command line), register the MCP server
in the AI, install the master instructions (and skill), install auto-capture hooks, install the instant-wake service,
keep models and the tool version current, hand over existing memories, then verify the connection.
"""
from __future__ import annotations

import getpass
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

STEPS = ["mcp", "instructions", "hooks", "wake", "sync", "import", "verify"]
LABELS = {"mcp": "connect it to Commons (MCP)", "instructions": "teach it the Commons routine (always-loaded instructions + skill)",
          "hooks": "auto-capture every message and tool call", "wake": "instant wake when work arrives",
          "sync": "keep this AI's models and tool version current",
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
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # private from the first byte
    with os.fdopen(fd, "w") as fh:
        fh.write(k + "\n")
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


def _claude_entry(url: str, helper: str) -> dict:
    """Claude Code's entry: the key is fetched by a headers helper at connect time, so ~/.claude.json never holds it."""
    return {"type": "http", "url": url, "headersHelper": helper}


def _hook_wrapper() -> str:
    """The command the Commons hooks run through in Claude Code (a secret-manager wrapper, or plain `commons`): the
    headers helper goes through the same door, so it finds the key exactly where the hooks do."""
    try:
        cfg = json.loads((Path.home() / ".claude" / "settings.json").read_text())
        for groups in (cfg.get("hooks") or {}).values():
            for g in groups:
                for h in g.get("hooks", []):
                    cmd = h.get("command", "")
                    if " hook claude-code " in cmd:
                        return cmd.split(" hook claude-code ")[0]
    except (OSError, ValueError, AttributeError):
        pass
    return ""


def mcp(client: str, url: str, key: str, agent: str = "") -> str:
    auth = f"Bearer {key}"
    if client == "claude-code":
        claude = shutil.which("claude") or shutil.which("claude", path=os.path.expanduser("~/.local/bin"))
        if not claude:
            return "skipped: claude CLI not on PATH"
        wrapper = _hook_wrapper()
        helper = f"{wrapper} mcp-auth" if wrapper else \
            f"COMMONS_AGENT={shlex.quote(agent)} {shlex.quote(sys.executable)} -m commons_memory mcp-auth"
        subprocess.run([claude, "mcp", "remove", "commons", "-s", "user"], capture_output=True)
        r = subprocess.run([claude, "mcp", "add-json", "--scope", "user", "commons", json.dumps(_claude_entry(url, helper))],
                           capture_output=True, text=True)
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
    keyfile.with_suffix(".json").write_text(json.dumps({"client": client, "url": base}))  # what a bare `commons doctor` checks
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
    step("mcp", lambda: mcp(client, mcp_url, key, agent))

    def instructions():
        from . import instructions as I
        if client not in I.TARGETS:
            return "this AI has no instruction file"
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

    def sync():
        r = subprocess.run([commons, "quota", "install", "--url", mcp_url, "--api-key-file", str(keyfile)],
                           capture_output=True, text=True)
        return (r.stdout.strip().splitlines() or ["installed"])[-1][:200] if r.returncode == 0 else f"failed: {r.stderr[-200:]}"
    step("sync", sync)

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


def ensure_mcp(client: str, url: str) -> str:
    """Session start: is Commons still registered as this client's MCP server? If not, put it back through the client's
    own CLI (in the background, so the session never waits) and say so in the session's context. '' = all well."""
    if client != "claude-code" or not url:
        return ""
    base = url.rstrip("/").removesuffix("/mcp")
    try:
        servers = json.loads((Path.home() / ".claude.json").read_text()).get("mcpServers") or {}
    except (OSError, ValueError, AttributeError):
        return ""  # missing or mid-write: look again next session
    if str((servers.get("commons") or {}).get("url", "")).rstrip("/").removesuffix("/mcp") == base:
        return ""
    wrapper = _hook_wrapper()
    claude = shutil.which("claude") or shutil.which("claude", path=os.path.expanduser("~/.local/bin"))
    if not (wrapper and claude):
        return "Commons is not connected as an MCP server here; run `commons doctor --repair` to connect it."
    subprocess.Popen([claude, "mcp", "add-json", "--scope", "user", "commons",
                      json.dumps(_claude_entry(base + "/mcp", f"{wrapper} mcp-auth"))],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return ("Commons was not registered as an MCP server in Claude Code, so its tools are missing from this session. "
            "It has been re-registered and the tools load in the next session; until then this context comes from the "
            "Commons hooks, and the hub answers over HTTP with the same key.")


def _mcp_config(client: str) -> str:
    cfg = {"claude-code": Path.home() / ".claude.json", "codex": Path.home() / ".codex" / "config.toml",
           "goose": Path.home() / ".config" / "goose" / "config.yaml",
           **{k: Path(os.path.expanduser(v[0])) for k, v in JSON_MCP.items()}}.get(client)
    try:
        return cfg.read_text() if cfg and cfg.exists() else ""
    except OSError:
        return ""


def registered(client: str, url: str) -> bool:
    """Is this client's MCP config already pointing at this hub?"""
    return url.rstrip("/").removesuffix("/mcp") in _mcp_config(client)


def elsewhere(client: str, url: str) -> bool:
    """Is this client already connected to a Commons hub at another address? `commons up` leaves such a client alone."""
    import re
    return not registered(client, url) and bool(  # an MCP entry named commons: JSON, codex TOML, goose YAML
        re.search(r'"commons"\s*:|mcp_servers\.commons\]|^\s*commons:\s*$', _mcp_config(client), re.M))


def doctor_all(repair: bool = False) -> int:
    """`commons doctor` with nothing else: every AI that setup connected on this machine."""
    home = Path(os.environ.get("COMMONS_HOME") or Path.home() / ".commons") / "agents"
    found = sorted(home.glob("*.json")) if home.is_dir() else []
    if not found:
        print("no AI on this machine was connected by `commons up`, `join` or `setup` since v0.42: "
              "run `commons up`, or name one with --client, --agent and --url")
        return 1
    rc = 0
    for f in found:
        c = json.loads(f.read_text())
        rc |= doctor(c["client"], f.stem, c["url"], repair)
    return rc


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
    add("MCP server registered", registered(client, base), client, "mcp")
    from .playbook import INSTRUCTIONS
    files = [Path(os.path.expanduser(p)) for p in I.TARGETS.get(client, [])]  # some clients have no instruction file
    if files:
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


def up(db: str, port: int = 8765, dash_port: int = 8766, connect: bool = True) -> int:
    """`commons up`: everything after the install, with nothing left to do by hand. Runs the hub and its dashboard as a
    login service on this machine (loopback only, every background job in-process), then connects each AI CLI found
    here: key, MCP, instructions, hooks, wake service, sync. Safe to run again: what is already set up is kept."""
    import platform
    import socket
    import time
    import urllib.request
    from . import expose as X
    from .cli_v7 import _install_unit
    from .connect import create_key
    from .join import CLIENT_BINS
    from .store import CommonsError, Store
    store = Store(db)
    base = f"http://127.0.0.1:{port}"
    argv = ["serve", "--transport", "http", "--host", "127.0.0.1", "--port", str(port), "--dashboard",
            "--dashboard-port", str(dash_port)]
    sysname = platform.system()
    kind = "launchd" if sysname == "Darwin" else "windows" if sysname == "Windows" else "systemd"
    env = {"COMMONS_BACKGROUND": os.environ.get("COMMONS_BACKGROUND", "full")}
    done = f"hub service installed: starts at login, MCP {base}/mcp, dashboard http://127.0.0.1:{dash_port}/"
    if kind == "windows":
        exe = shutil.which("commons") or sys.argv[0]
        tr = f'cmd /c "set COMMONS_DB={store.path}&& set COMMONS_BACKGROUND={env["COMMONS_BACKGROUND"]}&& "{exe}" {" ".join(argv)}"'
        subprocess.run(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", "Commons", "/TR", tr], capture_output=True)
        subprocess.run(["schtasks", "/Run", "/TN", "Commons"], capture_output=True)
        print(done)
    else:
        unit = X.service_unit(kind, port, "127.0.0.1", store.path, args=argv, env=env)
        mine = os.path.expanduser("~/Library/LaunchAgents/dev.commons.server.plist" if kind == "launchd"
                                  else "~/.config/systemd/user/commons.service")
        old = Path(mine).read_text() if os.path.exists(mine) else ""
        if old and not ("127.0.0.1" in old and str(port) in old):  # a hub someone set up another way (other port, on the network)
            print(f"a Commons hub service is already installed here with other settings ({mine}) and was left as it is.\n"
                  f"To replace it with the one-step setup, delete that file and run `commons up` again.", file=sys.stderr)
            return 1
        if kind == "launchd":
            path = os.path.expanduser("~/Library/LaunchAgents/dev.commons.server.plist")
            _install_unit(kind, "server", unit, files=[(path, unit)], done=done,
                          cmds=[["launchctl", "unload", path], ["launchctl", "load", "-w", path]])
        else:
            _install_unit(kind, "commons", unit, done=done, cmds=[
                ["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "commons"],
                ["systemctl", "--user", "restart", "commons"],
                ["loginctl", "enable-linger", os.environ.get("USER", "")]])  # keeps it up with nobody logged in
    def answers(tries: int) -> bool:
        for _ in range(tries):
            try:
                urllib.request.urlopen(base + "/bus/health", timeout=2).read()
                return True
            except OSError:
                time.sleep(0.5)
        return False
    if not answers(30):  # no user service manager here (a container, WSL without systemd): run it directly
        log = open(Path(store.path).parent / "commons.log", "a")
        subprocess.Popen([shutil.which("commons") or sys.argv[0], *argv], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         start_new_session=True, env=os.environ | env | {"COMMONS_DB": store.path, "COMMONS_REQUIRE_KEYS": "1"})
        if not answers(60):
            print(f"the hub did not answer on {base}; see {log.name}", file=sys.stderr)
            return 1
        print("this machine has no login service manager for your user, so the hub was started in the background instead: "
              "after a reboot, run `commons up` again")
    rc = 0
    if connect:
        host = "".join(c if c.isalnum() else "-" for c in socket.gethostname().split(".")[0].lower()).strip("-")[:24] or "host"
        keys = Path(os.environ.get("COMMONS_HOME") or Path.home() / ".commons") / "agents"
        found = [c for c, b in CLIENT_BINS.items() if shutil.which(b)]
        for c in found:
            if registered(c, base):
                print(f"\n== {c}: already connected")
                continue
            if elsewhere(c, base):
                print(f"\n== {c}: already connected to another Commons hub, left as it is "
                      f"(to move it here: `commons connect {c} --url {base}`)")
                continue
            n = f"{c}-{host}"
            if not (keys / f"{n}.key").exists():
                try:
                    k = create_key(store, n, platform=c)["api_key"]
                except CommonsError as e:
                    print(f"\n== {c}: {e} (its key file is gone: `commons connect {c} --name {n} --new-key`)")
                    rc = 1
                    continue
                keys.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(keys / f"{n}.key", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as fh:
                    fh.write(k + "\n")
            print(f"\n== {c} as {n}")
            rc |= run(c, n, base, set())
        if not found:
            print("no AI CLI found here yet (claude, codex, gemini, cursor-agent, ...): install one and run `commons up` again")
    print(f"\nCommons is running and starts by itself from now on. Dashboard, whenever you want to look: http://127.0.0.1:{dash_port}/")
    return rc
