"""v0.8.1 MCP tools: whoami (connection check used by the Connect page's verify command)."""
from __future__ import annotations

import json

from . import __version__


def register_v81(mcp, store, who):
    @mcp.tool()
    def whoami(agent: str = "", api_key: str = "") -> str:
        """Connection check: returns which agent this key belongs to and the server version, plus the marker
        `commons-ok`. Cheap; safe to call any time."""
        aid, authed = who(agent, api_key, write=False)
        a = store.get_agent(aid) if aid else None
        return json.dumps({"status": "commons-ok", "agent": a["name"] if a else None, "id": aid,
                           "authenticated": authed, "version": __version__})
