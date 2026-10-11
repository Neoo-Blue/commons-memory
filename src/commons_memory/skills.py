"""`commons install-skill <platform>` and `commons connect`: make every agent platform speak Commons."""
from __future__ import annotations

import json
import os
import sys
import shutil
import stat
from importlib import resources
from pathlib import Path

PLATFORMS = ["claude-code", "claude-desktop", "cursor", "codex", "agents-md", "chatgpt", "grok", "muse", "generic"]


def _res(name: str) -> str:
    return (resources.files("commons_memory") / "skills" / name).read_text(encoding="utf-8")


def _mcp_entry(agent: str, key: str | None = None, db: str | None = None) -> dict:
    env = {"COMMONS_AGENT": agent}
    if key:
        env["COMMONS_API_KEY"] = key
    if db:
        env["COMMONS_DB"] = db
    cmd = shutil.which("commons") or "commons"
    return {"command": cmd, "args": ["serve"], "env": env}


def _merge_json(path: Path, agent: str, key: str | None, db: str | None) -> None:
    data = json.loads(path.read_text()) if path.exists() and path.read_text().strip() else {}
    data.setdefault("mcpServers", {})["commons"] = _mcp_entry(agent, key, db)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _append_section(path: Path, text: str, marker: str = "## Commons") -> bool:
    old = path.read_text() if path.exists() else ""
    if marker in old:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((old.rstrip() + "\n\n" if old else "") + text)
    return True


def install(platform: str, *, project_dir: str = ".", scope: str = "project", agent: str | None = None,
            key: str | None = None, db: str | None = None, home: str | None = None) -> list[str]:
    """Write skill/rules/config files for a platform. Returns human-readable actions."""
    platform = platform.lower()
    home_p = Path(home or Path.home())
    proj = Path(project_dir).resolve()
    agent = agent or platform
    done: list[str] = []
    if platform == "claude-code":
        base = (home_p / ".claude") if scope == "user" else (proj / ".claude")
        sk = base / "skills" / "commons" / "SKILL.md"
        sk.parent.mkdir(parents=True, exist_ok=True)
        sk.write_text(_res("SKILL.md"))
        done.append(f"skill -> {sk}")
        cmd_dir = base / "commands"
        cmd_dir.mkdir(parents=True, exist_ok=True)
        for f in (resources.files("commons_memory") / "skills" / "claude-code" / "commands").iterdir():
            (cmd_dir / f.name).write_text(f.read_text(encoding="utf-8"))
            done.append(f"slash command /{f.name[:-3]} -> {cmd_dir / f.name}")
        plug = base / "plugins" / "commons"
        (plug / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (plug / ".claude-plugin" / "plugin.json").write_text(_res("claude-code/plugin.json"))
        done.append(f"plugin manifest -> {plug / '.claude-plugin' / 'plugin.json'}")
        if scope == "user":
            done.append(f"MCP: run  claude mcp add commons -s user -e COMMONS_AGENT={agent}"
                        + (f" -e COMMONS_API_KEY={key}" if key else "") + " -- commons serve")
        else:
            _merge_json(proj / ".mcp.json", agent, key, db)
            done.append(f"MCP server -> {proj / '.mcp.json'}")
    elif platform == "claude-desktop":
        cfg = {"darwin": home_p / "Library/Application Support/Claude/claude_desktop_config.json"}.get(
            sys.platform, home_p / ".config/Claude/claude_desktop_config.json")
        if os.name == "nt":
            cfg = Path(os.environ.get("APPDATA", home_p)) / "Claude" / "claude_desktop_config.json"
        _merge_json(cfg, agent, key, db)
        done.append(f"MCP server -> {cfg} (restart Claude Desktop)")
        done.append("Paste skills/system-prompt.md into a Claude Project's instructions, or upload SKILL.md as a skill")
    elif platform == "cursor":
        rule = proj / ".cursor" / "rules" / "commons.mdc"
        rule.parent.mkdir(parents=True, exist_ok=True)
        rule.write_text(_res("cursor-rule.mdc"))
        done.append(f"rule -> {rule}")
        mcp = (home_p / ".cursor" / "mcp.json") if scope == "user" else (proj / ".cursor" / "mcp.json")
        _merge_json(mcp, agent, key, db)
        done.append(f"MCP server -> {mcp}")
    elif platform in ("codex", "agents-md"):
        target = proj / "AGENTS.md"
        changed = _append_section(target, _res("AGENTS.md"))
        done.append(f"AGENTS.md section {'added to' if changed else 'already present in'} {target}")
        if platform == "codex":
            cfg = home_p / ".codex" / "config.toml"
            old = cfg.read_text() if cfg.exists() else ""
            if "[mcp_servers.commons]" not in old:
                e = _mcp_entry(agent, key, db)
                env = ", ".join(f'{k} = "{v}"' for k, v in e["env"].items())
                cfg.parent.mkdir(parents=True, exist_ok=True)
                cfg.write_text(old + f'\n[mcp_servers.commons]\ncommand = "{e["command"]}"\nargs = ["serve"]\n'
                                     f"env = {{ {env} }}\n")
                done.append(f"MCP server -> {cfg}")
            else:
                done.append(f"MCP server already configured in {cfg}")
    elif platform in ("chatgpt", "grok", "muse", "generic"):
        out = proj / f"commons-{platform}-prompt.md"
        out.write_text(_res("system-prompt.md"))
        done.append(f"system prompt -> {out} (paste into custom instructions / project instructions / system prompt)")
        done.append("Connect over HTTP: `commons serve --transport http --host 0.0.0.0` behind TLS, then add "
                    "https://<host>/mcp as a remote MCP server/connector; pass the agent api_key on writes")
    else:
        raise ValueError(f"unknown platform {platform}; choose from {', '.join(PLATFORMS)}")
    return done


def save_key(agent: str, key: str, home: str | None = None) -> Path:
    d = Path(home or Path.home()) / ".commons" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{agent}.key"
    p.write_text(key)
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    return p
