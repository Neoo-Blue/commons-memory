"""Storage backend interface. SQLite (store.Store) is the default implementation.

A Postgres + pgvector adapter is defined as an interface + DDL for v2; it is not wired up in v1.
"""
from __future__ import annotations

from typing import Any, Protocol


class StorageBackend(Protocol):
    def add_item(self, kind: str, content: str, **kw: Any) -> dict: ...
    def get_item(self, iid: str) -> dict | None: ...
    def search(self, query: str = "", **kw: Any) -> dict: ...
    def upsert_entity(self, type_: str, name: str, project_id: str | None = None, path: str | None = None,
                      line: int | None = None, meta: dict | None = None) -> str: ...
    def relate(self, src: str, dst: str, type_: str, meta: dict | None = None, agent_id: str | None = None) -> str: ...
    def neighbors(self, any_id: str, depth: int = 1, **kw: Any) -> dict: ...


PG_DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS items(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, project_id TEXT, agent_id TEXT, session_id TEXT,
  title TEXT, summary TEXT, content TEXT, tags JSONB, meta JSONB, subject TEXT, predicate TEXT, value TEXT,
  conflict BOOLEAN DEFAULT FALSE, superseded_by TEXT, authenticated BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now(),
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(title,'')||' '||coalesce(content,''))) STORED,
  embedding vector(384));
CREATE INDEX IF NOT EXISTS items_tsv ON items USING gin(tsv);
CREATE INDEX IF NOT EXISTS items_vec ON items USING hnsw (embedding vector_cosine_ops);
-- entities, relations, sessions, agents, inbox, leases, audit mirror the SQLite schema in store.py
"""


class PostgresBackend:  # pragma: no cover - v2
    """Postgres/pgvector adapter (interface only in v1). Implement using psycopg and PG_DDL."""

    def __init__(self, dsn: str):
        raise NotImplementedError("Postgres backend is planned for v2; use the default SQLite store")
