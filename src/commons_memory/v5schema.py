"""v0.5 tables (applied by schema_v2.migrate)."""
SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS agent_trust(agent_id TEXT PRIMARY KEY, score REAL, components TEXT, manual REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS a2a_tasks(id TEXT PRIMARY KEY, context_id TEXT, job_id TEXT, status TEXT, caller TEXT,
  message TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS usage_daily(day TEXT, ts REAL, agent_id TEXT, project_id TEXT, model TEXT, tokens_in INTEGER,
  tokens_out INTEGER, cost REAL, seconds REAL, calls INTEGER, cached_tokens INTEGER,
  PRIMARY KEY(day, agent_id, project_id, model));
CREATE TABLE IF NOT EXISTS escalations(id TEXT PRIMARY KEY, subtask_id TEXT, job_id TEXT, level TEXT, action TEXT,
  detail TEXT, created_at REAL, resolved_at REAL);
CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY, ts REAL, level TEXT, title TEXT, body TEXT, target TEXT,
  read INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS mcp_agent_creds(server TEXT, agent_id TEXT, env_refs TEXT, header_refs TEXT,
  PRIMARY KEY(server, agent_id));
CREATE TABLE IF NOT EXISTS routing_validation(id TEXT PRIMARY KEY, task_type TEXT, old_model TEXT, new_model TEXT,
  applied_at REAL, baseline REAL, status TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS token_count_cache(key TEXT PRIMARY KEY, tokens INTEGER, method TEXT, ts REAL);
"""

ADD_COLUMNS = [("recurring", "catchup", "TEXT DEFAULT 'once'"), ("recurring", "max_catchup", "INTEGER DEFAULT 3"),
               ("leases", "backend_lease", "TEXT"), ("subtasks", "escalation", "TEXT")]


def add_columns(db):
    for t, c, typ in ADD_COLUMNS:
        cols = {r[1] for r in db.execute(f"PRAGMA table_info({t})")}
        if cols and c not in cols:
            db.execute(f"ALTER TABLE {t} ADD COLUMN {c} {typ}")
