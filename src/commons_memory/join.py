"""Join links: one line connects a machine's AIs to Commons, and every one of them can be woken from then on.

The dashboard makes a join code (15 minutes, up to 5 agents). `curl -fsSL <hub>/join/<code> | sh` installs the commons
client and runs `commons join`, which finds the AIs on that machine, claims a key for each, and runs the full setup
(MCP, instructions, hooks, wake service, sync, verify). A bot reads <hub>/join/<code>/agent and follows it: it names the
command that wakes it, and Commons runs that through a wake service that holds a connection open to the hub, so it is
woken through NAT, the tailnet or a firewall with no port opened. Nobody copies or pastes a key."""
from __future__ import annotations

import json
import re
import secrets

from .ids import iso, now
from .store import CommonsError, Store

TTL, USES = 900, 5
REPO = "Neoo-Blue/commons-memory"

SCHEMA = """CREATE TABLE IF NOT EXISTS join_codes(code TEXT PRIMARY KEY, created_at REAL, expires_at REAL, uses_left INTEGER,
  role TEXT, project TEXT, claims TEXT, hub TEXT);"""


def ensure(s: Store) -> None:
    if not getattr(s, "_join_ok", False):
        s.x(SCHEMA)
        s._join_ok = True


def create(s: Store, role: str = "write", project: str = "", ttl: int = TTL, uses: int = USES, hub: str = "") -> dict:
    """hub = the address the owner picked: the installer and the bot page use it, never a request's Host header."""
    ensure(s)
    code = secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
    hub = _safe_hub(hub.rstrip("/").removesuffix("/mcp")) if hub else ""
    s.x("INSERT INTO join_codes(code,created_at,expires_at,uses_left,role,project,claims,hub) VALUES(?,?,?,?,?,?,?,?)",
        (code, now(), now() + ttl, uses, role, project, "[]", hub))
    s.audit(None, "join.create", code[:4] + "…", {"role": role, "uses": uses})
    return {"code": code, "expires_at": iso(now() + ttl), "uses": uses}


def _live(s: Store, code: str) -> dict:
    ensure(s)
    r = s.q("SELECT * FROM join_codes WHERE code=?", (code or "",))
    if not r or r[0]["expires_at"] < now():
        raise CommonsError("this join link has expired or doesn't exist: make a new one on the dashboard (Agents > Connect)")
    return dict(r[0])


def _taken(s: Store, name: str) -> bool:
    """A join code only ever makes a NEW agent: never an existing row (even a keyless placeholder such as `user`, an
    OAuth or SSO principal, or an agt_ id), a hub identity, an admin name, or someone's display name."""
    import os
    from .team import taken
    admins = {x.strip().casefold() for x in os.environ.get("COMMONS_ADMIN_AGENTS", "").split(",") if x.strip()}
    return (bool(s.get_agent(name)) or name.casefold().startswith("agt_") or name.casefold() in admins
            or taken(s, name))


def claim(s: Store, code: str, name: str, client: str = "", host: str = "") -> dict:
    """Trade the join code for a NEW agent's key (once per agent, up to the code's limit)."""
    from .connect import create_key
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-")[:48]
    if len(name) < 2:
        raise CommonsError("name the agent (letters, digits, dot, dash), e.g. claude-code-macbook")
    client, host = re.sub(r"[^A-Za-z0-9._-]", "", client or "")[:40], re.sub(r"[^A-Za-z0-9._-]", "", host or "")[:80]
    if _live(s, code)["uses_left"] <= 0:  # checked before taking the write lock, so a bogus code can't hold it
        raise CommonsError("this join link has been used up: make a new one on the dashboard")
    # ponytail: one hub process (SQLite) serialises claims here; several processes on Postgres need a conditional
    # UPDATE ... WHERE uses_left>0 with a row count, if that deployment ever appears
    with s.tx():  # one claim at a time: two at once can't both take the last use
        c = _live(s, code)
        if c["uses_left"] <= 0:
            raise CommonsError("this join link has been used up: make a new one on the dashboard")
        if _taken(s, name):
            raise CommonsError(f"{name!r} is taken: pick another name (an agent that already has a key keeps it: "
                               "`commons join` with --agent and COMMONS_API_KEY)")
        reg = create_key(s, name, role=c["role"] or "write", project=c["project"] or "", platform=client)
        s.x("UPDATE agents SET last_seen=? WHERE id=?", (now(), reg["id"]))
        claims = json.loads(c["claims"] or "[]") + [{"name": name, "client": client, "host": host, "at": iso(now())}]
        s.x("UPDATE join_codes SET uses_left=uses_left-1, claims=? WHERE code=?", (json.dumps(claims), code))
    s.audit(reg["id"], "join.claim", reg["id"], {"client": client, "host": host[:80]})
    return {"name": name, "api_key": reg["api_key"]}


def close(s: Store, code: str) -> dict:
    """End a link early: nothing more can join with it."""
    ensure(s)
    s.x("UPDATE join_codes SET uses_left=0, expires_at=MIN(expires_at, ?) WHERE code=?", (now(), code))
    return {"closed": True}


def hub_of(s: Store, code: str, fallback: str) -> str:
    r = s.q("SELECT hub FROM join_codes WHERE code=?", (code or "",)) if code else []
    return (r[0]["hub"] if r and r[0]["hub"] else "") or fallback


def status(s: Store, code: str) -> dict:
    """Who has joined with this code, and whether Commons can wake each of them."""
    from .messaging import presence
    ensure(s)
    r = s.q("SELECT * FROM join_codes WHERE code=?", (code or "",))
    if not r:
        raise CommonsError("unknown join code")
    out = []
    for c in json.loads(r[0]["claims"] or "[]"):
        a = s.get_agent(c["name"])
        p = presence(s, a["id"]) if a else {}
        out.append(c | {"state": p.get("state"), "wakeable": bool(p.get("wakeable")), "wake_mode": p.get("wake_mode")})
    return {"code": code, "expires_at": iso(r[0]["expires_at"]), "expired": r[0]["expires_at"] < now(),
            "uses_left": r[0]["uses_left"], "joined": out}


def _safe_hub(hub: str) -> str:
    if not re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]+(/[A-Za-z0-9._~/-]*)?", hub or ""):
        raise CommonsError("the hub address has unexpected characters: open the dashboard by its plain address")
    return hub


def script(s: Store, code: str, hub: str) -> str:
    """The installer `curl | sh` runs: get the commons client, then hand over to `commons join`."""
    hub = _safe_hub(hub_of(s, code, hub))
    c = _live(s, code)
    from . import __version__
    return f"""#!/bin/sh
# Commons: connect the AIs on this machine to the hub at {hub}
# (join link {code[:4]}…, valid until {iso(c['expires_at'])}). Read it first? Save it instead of piping to sh.
set -eu
HUB="{hub}"
CODE="{code}"
say() {{ printf '\\033[1;35mcommons:\\033[0m %s\\n' "$*"; }}
have() {{ command -v "$1" >/dev/null 2>&1; }}
PATH="$HOME/.local/bin:$PATH"; export PATH
if ! have commons; then
  SPEC="commons-memory[http] @ git+https://github.com/{REPO}@v{__version__}"
  say "installing the commons client (v{__version__})"
  if have uv; then uv tool install --quiet "$SPEC"
  elif have pipx; then pipx install --quiet "$SPEC"
  elif have python3; then python3 -m pip install --user --quiet "$SPEC" || python3 -m pip install --user --quiet --break-system-packages "$SPEC"
  else curl -LsSf https://astral.sh/uv/install.sh | sh && "$HOME/.local/bin/uv" tool install --quiet "$SPEC"
  fi
fi
exec commons join "$HUB" "$CODE" "$@"
"""


def agent_page(s: Store, code: str, hub: str, existing: str = "") -> str:
    """What a bot (or any AI with a shell) reads to join on its own."""
    hub = _safe_hub(hub_of(s, code, hub))
    existing = re.sub(r"[^A-Za-z0-9._-]", "", existing or "")[:48]
    _live(s, code)
    if existing:
        intro = (f"You are already the Commons agent `{existing}`, and Commons can't wake you yet. Keep your key: this only "
                 "installs a wake service that keeps a connection open to the hub and runs your wake command when work or "
                 "a message arrives. Run this on the machine you run on:")
        cmd = (f"export COMMONS_API_KEY=<your Commons key>\n    curl -fsSL {hub}/join/{code} | sh -s -- --agent {existing} "
               "--wake-cmd '<command that wakes you>'")
    else:
        intro = ("The user is connecting you to Commons, the shared memory, task and credential hub for all of their AIs. "
                 "Run this one command on the machine you run on (it installs the commons client, gets your agent key, "
                 "and installs a wake service so Commons can reach you at any time):")
        cmd = f"curl -fsSL {hub}/join/{code} | sh -s -- --name <your-name> --wake-cmd '<command that wakes you>'"
    return f"""# Join Commons at {hub}

{intro}

    {cmd}

- `<your-name>` (new agents): a short handle, e.g. grok-bot (letters, digits, dash).
- `<command that wakes you>`: a shell command that starts you working, with the task text on standard input. For a
  CLI agent that is its headless mode, e.g. `claude -p`; for a bot with a local wake endpoint, something like
  `curl -s -X POST http://127.0.0.1:8765/wake --data-binary @-`. Commons runs it each time work or a message arrives,
  through a service that keeps a connection open to the hub, so you are woken even behind NAT or a firewall.

Then call pickup() and msg_recv() on Commons (MCP at {hub}/mcp, your key as the Bearer token) and follow the playbook it
returns.
"""


# ------------------------------------------------------------------------------------------------ on the machine
# client -> the CLI that shows it is installed here (the setup clients that can be woken headless)
CLIENT_BINS = {"claude-code": "claude", "codex": "codex", "gemini": "gemini", "cursor": "cursor-agent",
               "opencode": "opencode", "copilot": "copilot", "goose": "goose", "qwen": "qwen", "crush": "crush",
               "amp": "amp", "factory": "droid", "kilo": "kilo", "cline": "cline"}


def here(hub: str, code: str, name: str = "", wake_cmd: str = "", agent: str = "") -> int:
    """`commons join HUB CODE`: connect every AI CLI on this machine (or one bot) and make each wakeable."""
    import os
    import shutil
    import socket
    import subprocess
    import sys
    import urllib.error
    import urllib.request
    from pathlib import Path
    from . import setup as SU
    hub = hub.rstrip("/").removesuffix("/mcp")
    host = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().split(".")[0].lower()).strip("-")[:24] or "host"
    keys = Path(os.environ.get("COMMONS_HOME") or Path.home() / ".commons") / "agents"
    def save(n: str, key: str) -> None:
        keys.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(keys / f"{n}.key", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(key.strip() + "\n")
    def take(n: str, client: str) -> str:  # a key from the join code, unless this machine already holds one for n
        if (keys / f"{n}.key").exists():
            return n
        req = urllib.request.Request(f"{hub}/join/{code}/claim", method="POST", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"name": n, "client": client, "host": host}).encode())
        try:
            r = json.load(urllib.request.urlopen(req, timeout=30))
        except urllib.error.HTTPError as e:
            raise SystemExit("commons join: " + (json.loads(e.read() or b"{}").get("error") or str(e))) from None
        save(r["name"], r["api_key"])
        return r["name"]
    commons = shutil.which("commons") or sys.argv[0]
    done: list[tuple[str, str, str]] = []
    if wake_cmd or agent or name:  # one agent: a bot, or an agent that already has a key and needs its wake service
        if not wake_cmd:
            raise SystemExit("commons join: --wake-cmd is required, so Commons can wake this agent whenever work arrives "
                             "(the command gets the task on stdin), e.g. --wake-cmd 'claude -p'")
        if agent:
            if os.environ.get("COMMONS_API_KEY"):
                save(agent, os.environ["COMMONS_API_KEY"])
            elif not (keys / f"{agent}.key").exists():
                raise SystemExit(f"commons join: export COMMONS_API_KEY=<{agent}'s key> first; it keeps the key it has")
            n = agent
        else:
            n = take(name or f"agent-{host}", "bot")
        rc = subprocess.run([commons, "agent", "daemon", "--agent", n, "--url", hub, "--cmd", wake_cmd, "--install"]).returncode
        done.append((n, "bot", "connected, wake service installed" if rc == 0 else "connected; wake service FAILED (see above)"))
    else:
        found = [c for c, b in CLIENT_BINS.items() if shutil.which(b)]
        if not found:
            raise SystemExit("commons join: no AI CLI found here (claude, codex, gemini, cursor-agent, opencode, ...). "
                             "For a bot: commons join HUB CODE --name <name> --wake-cmd '<command that wakes it>'")
        for c in found:
            if SU.registered(c, hub):
                done.append((c, c, "already connected to this hub"))
                continue
            n = take(f"{c}-{host}", c)
            print(f"\n== {c} as {n}")
            rc = SU.run(c, n, hub, set())
            done.append((n, c, "connected and wakeable" if rc == 0 else "connected with problems (see above)"))
    print("\nCommons:")
    for n, c, how in done:
        print(f"  {n:32} {how}")
    print(f"Open the dashboard to see them: {hub.replace(':8770', ':8771')}")
    return 0
