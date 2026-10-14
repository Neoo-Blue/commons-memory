"""v0.8.1 MCP tools: whoami (connection check used by the Connect page's verify command)."""
from __future__ import annotations

import json

from . import __version__


def register_v81(mcp, store, who):
    @mcp.tool()
    def infra(query: str = "", agent: str = "", api_key: str = "") -> str:
        """Where things run and how they connect: the infrastructure map of machines, LXCs/VMs, containers, services,
        domains, tunnels, storage, repos and which vault credential each uses (names only). infra() gives the whole tree;
        infra("plex"), infra("LXC 106") or infra("192.168.50.205") gives one thing with what it runs on, exposes, stores
        and talks to, and where each fact came from."""
        who(agent, api_key, write=False)
        from .infra import describe
        return describe(store, query)

    @mcp.tool()
    def whoami(agent: str = "", api_key: str = "") -> str:
        """Connection check: returns which agent this key belongs to and the server version, plus the marker
        `commons-ok`. Cheap; safe to call any time."""
        aid, authed = who(agent, api_key, write=False)
        a = store.get_agent(aid) if aid else None
        return json.dumps({"status": "commons-ok", "agent": a["name"] if a else None, "id": aid,
                           "authenticated": authed, "version": __version__})
