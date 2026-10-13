"""Postgres + pgvector backend.

Commons' modules speak a small SQLite dialect. ``PgDB`` is a drop-in for the ``sqlite3.Connection`` that ``Store``
uses: it translates that dialect to Postgres (placeholders, INSERT OR REPLACE/IGNORE -> ON CONFLICT, AUTOINCREMENT ->
BIGSERIAL, PRAGMA table_info, ``IS ?``, case-insensitive LIKE, FTS5 -> tsvector + GIN) and returns sqlite3.Row-like
rows. Store adds a pgvector column + HNSW index on embeddings and does keyword and vector search in the database.

Use it with ``COMMONS_DB=postgresql://user:pass@host/db`` (install ``commons-memory[postgres]``).
Optional ``?schema=name`` puts all tables in one schema (handy for tests / multi-tenant).
"""
from __future__ import annotations

import re
import threading
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


def is_pg(path: str | None) -> bool:
    return bool(path) and str(path).startswith(("postgres://", "postgresql://"))


def _conv(v):
    from decimal import Decimal
    if isinstance(v, memoryview):
        return bytes(v)
    if isinstance(v, Decimal):  # SUM(bigint) -> numeric; sqlite gives int/float
        return int(v) if v == v.to_integral_value() else float(v)
    return v


class Row(tuple):
    """tuple + mapping access, like sqlite3.Row."""

    def __new__(cls, values, keys):
        r = super().__new__(cls, [_conv(v) for v in values])
        r._k = keys
        return r

    def keys(self):
        return list(self._k)

    def __getitem__(self, k):
        if isinstance(k, str):
            try:
                return tuple.__getitem__(self, self._k.index(k))
            except ValueError:
                raise IndexError(f"No item with that key: {k}") from None
        return tuple.__getitem__(self, k)


class _Cur:
    def __init__(self, rows, rowcount, lastrowid):
        self._rows, self.rowcount, self.lastrowid = rows, rowcount, lastrowid

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


def _split_sql(script: str) -> list[str]:
    out, cur, q = [], [], None
    for ch in script:
        if q:
            q = None if ch == q else q
        elif ch in "'\"":
            q = ch
        elif ch == ";":
            out.append("".join(cur)); cur = []; continue
        cur.append(ch)
    out.append("".join(cur))
    return [s.strip() for s in out if s.strip()]


def _placeholders(sql: str) -> str:
    """'?' -> '%s' and '%' -> '%%' outside string literals; IS ? -> IS NOT DISTINCT FROM."""
    out, q, i = [], None, 0
    while i < len(sql):
        ch = sql[i]
        if q:
            out.append("%%" if ch == "%" else ch)
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch; out.append(ch)
        elif ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
        i += 1
    s = "".join(out)
    s = re.sub(r"\bIS\s+NOT\s+%s", "IS DISTINCT FROM %s", s, flags=re.I)
    s = re.sub(r"\bIS\s+%s", "IS NOT DISTINCT FROM %s", s, flags=re.I)
    s = re.sub(r"(?<![\w])LIKE\b", "ILIKE", s)
    return s


def _ddl(stmt: str) -> list[str]:
    s = stmt.strip()
    if re.match(r"PRAGMA\s+(journal_mode|foreign_keys|synchronous|busy_timeout)", s, re.I):
        return []
    m = re.match(r"CREATE VIRTUAL TABLE IF NOT EXISTS (\w+) USING fts5\((.*)\)\s*$", s, re.I | re.S)
    if m:
        name = m.group(1)
        cols = [c.split()[0] for c in m.group(2).split(",") if "=" not in c]
        body = ", ".join(f"{c} TEXT" for c in cols)
        txt = "||' '||".join(f"coalesce({c},'')" for c in cols[1:])
        return [f"CREATE TABLE IF NOT EXISTS {name}({body}, tsv tsvector GENERATED ALWAYS AS "
                f"(to_tsvector('english', {txt})) STORED)",
                f"CREATE INDEX IF NOT EXISTS {name}_tsv ON {name} USING gin(tsv)",
                f"CREATE INDEX IF NOT EXISTS {name}_id ON {name}({cols[0]})"]
    s = re.sub(r"\bINTEGER PRIMARY KEY AUTOINCREMENT\b", "BIGSERIAL PRIMARY KEY", s, flags=re.I)
    if re.match(r"(CREATE TABLE|ALTER TABLE)", s, re.I):
        s = re.sub(r"\bREAL\b", "DOUBLE PRECISION", s)
        s = re.sub(r"\bBLOB\b", "BYTEA", s)
        s = re.sub(r"\bINTEGER\b", "BIGINT", s)
        s = re.sub(r"ADD COLUMN (?!IF NOT EXISTS)", "ADD COLUMN IF NOT EXISTS ", s, flags=re.I)
    return [s]


class PgDB:
    dialect = "postgres"

    def __init__(self, dsn: str):
        import psycopg  # optional dependency
        u = urlparse(dsn)
        qs = parse_qs(u.query)
        self.schema = (qs.pop("schema", [None])[0])
        dsn = urlunparse(u._replace(query=urlencode({k: v[0] for k, v in qs.items()})))
        self.conn = psycopg.connect(dsn, autocommit=True)
        self.lock = threading.RLock()
        if self.schema:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.schema):
                raise ValueError("bad schema name")
            self.conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
            self.conn.execute(f'SET search_path TO "{self.schema}", public')
        self.conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        self._meta: dict[str, tuple[list[str], list[str], bool]] = {}
        self._cache: dict[str, tuple[str, str | None]] = {}

    # -- table metadata (columns, primary key, serial id)
    def _table(self, t: str):
        if t not in self._meta:
            cols = [r[0] for r in self.conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name=%s AND table_schema=current_schema()"
                " AND is_generated='NEVER' ORDER BY ordinal_position", (t,)).fetchall()]
            pk = [r[0] for r in self.conn.execute(
                "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey)"
                " WHERE i.indrelid=to_regclass(%s) AND i.indisprimary", (t,)).fetchall()]
            serial = bool(self.conn.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND table_schema=current_schema() "
                "AND column_name='id' AND column_default LIKE 'nextval%%'", (t,)).fetchall())
            self._meta[t] = (cols, pk, serial)
        return self._meta[t]

    def _translate(self, sql: str) -> tuple[str, str | None]:
        if sql in self._cache:
            return self._cache[sql]
        s = sql.strip()
        ret_table = None
        m = re.match(r"PRAGMA\s+table_info\((\w+)\)", s, re.I)
        if m:
            out = (f"SELECT ordinal_position-1 AS cid, column_name AS name, data_type AS type, 0 AS notnull, "
                   f"NULL AS dflt_value, 0 AS pk FROM information_schema.columns WHERE table_name='{m.group(1)}' "
                   f"AND table_schema=current_schema() ORDER BY ordinal_position")
            self._cache[sql] = (out, None)
            return out, None
        if re.match(r"PRAGMA\b", s, re.I):
            return "", None
        if re.match(r"(CREATE|ALTER|DROP)\b", s, re.I):
            out = ";\n".join(_ddl(s))
            return out, None  # not cached: DDL changes metadata
        m = re.match(r"INSERT\s+OR\s+(REPLACE|IGNORE)\s+INTO\s+(\w+)\s*(\(([^)]*)\))?\s*(VALUES.*)$", s, re.I | re.S)
        if m:
            kind, t, cols_s, rest = m.group(1).upper(), m.group(2), m.group(4), m.group(5)
            allcols, pk, _ = self._table(t)
            cols = [c.strip() for c in cols_s.split(",")] if cols_s else allcols
            head = f"INSERT INTO {t}({','.join(cols)}) {rest}"
            if kind == "IGNORE":
                s = head + " ON CONFLICT DO NOTHING"
            elif pk:
                upd = [c for c in cols if c not in pk]
                s = head + f" ON CONFLICT ({','.join(pk)}) DO " + (
                    "UPDATE SET " + ", ".join(f"{c}=EXCLUDED.{c}" for c in upd) if upd else "NOTHING")
            else:
                s = head
        m = re.match(r"INSERT\s+INTO\s+(\w+)", s, re.I)
        if m and self._table(m.group(1))[2] and "RETURNING" not in s.upper():
            s += " RETURNING id"
            ret_table = m.group(1)
        s = re.sub(r"\bBEGIN\s+(IMMEDIATE|EXCLUSIVE|DEFERRED)\b", "BEGIN", s, flags=re.I)
        # SQLite's scalar MAX(a,b)/MIN(a,b) are GREATEST/LEAST in Postgres (aggregate MAX(x) has no comma)
        s = re.sub(r"\b(MAX|MIN)\(([^()]*,[^()]*)\)",
                   lambda m: ("GREATEST(" if m.group(1).upper() == "MAX" else "LEAST(") + m.group(2) + ")", s, flags=re.I)
        out = (_placeholders(s), ret_table)
        self._cache[sql] = out
        return out

    @property
    def in_transaction(self) -> bool:  # same meaning as sqlite3.Connection.in_transaction
        from psycopg.pq import TransactionStatus
        return self.conn.info.transaction_status != TransactionStatus.IDLE

    def execute(self, sql: str, params=()) -> _Cur:
        with self.lock:
            q, ret = self._translate(sql)
            if not q:
                return _Cur([], 0, None)
            if ret is None and re.match(r"(CREATE|ALTER|DROP)\b", q, re.I):
                for st in q.split(";\n"):
                    self.conn.execute(st)
                self._meta.clear()
                return _Cur([], 0, None)
            cur = self.conn.execute(q, tuple(params))
            rows, last = [], None
            if cur.description:
                keys = [d.name for d in cur.description]
                rows = [Row(r, keys) for r in cur.fetchall()]
                if ret and rows:
                    last = rows[0][0]
                    rows = []
            return _Cur(rows, cur.rowcount, last)

    def executemany(self, sql: str, seq) -> _Cur:
        n = 0
        for params in seq:
            n += max(0, self.execute(sql, params).rowcount or 0)
        return _Cur([], n, None)

    def executescript(self, script: str) -> None:
        for st in _split_sql(script):
            self.execute(st)

    def close(self):
        self.conn.close()


def migrate_sqlite_to_pg(sqlite_path: str, dsn: str) -> dict:
    """Copy every table from a SQLite Commons DB into Postgres (schema is created first)."""
    import sqlite3

    from .store import Store
    dst = Store(dsn, embedder="none")
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    counts = {}
    tables = [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'items_fts%' "
                                        "AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        rows = src.execute(f"SELECT * FROM {t}").fetchall()
        if not rows:
            continue
        cols = rows[0].keys()
        dcols = set(dst.db._table(t)[0])
        cols = [c for c in cols if c in dcols]
        sql = f"INSERT OR IGNORE INTO {t}({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
        for r in rows:
            dst.db.execute(sql, [r[c] for c in cols])
        counts[t] = len(rows)
    for r in src.execute("SELECT item_id,title,summary,content,tags FROM items_fts"):
        dst.db.execute("INSERT INTO items_fts(item_id,title,summary,content,tags) VALUES(?,?,?,?,?)", tuple(r))
    for t in tables:  # bump serial sequences past copied ids
        if dst.db._table(t)[2]:
            dst.db.conn.execute(f"SELECT setval(pg_get_serial_sequence('{t}','id'), COALESCE((SELECT MAX(id) FROM {t}),0)+1, false)")
    dst.sync_vectors()
    return counts
