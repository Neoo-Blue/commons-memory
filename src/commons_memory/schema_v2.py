"""v2 tables: capability profiles/outcomes, jobs + subtask DAG, leftovers, recurring tasks, preferences."""
SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS outcomes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT, task_type TEXT, success INTEGER, rating REAL,
  subtask_id TEXT, tokens INTEGER, seconds REAL, note TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS outcomes_agent ON outcomes(agent_id, task_type);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, title TEXT, body TEXT, pattern TEXT, project_id TEXT, submitted_by TEXT, status TEXT,
  budget TEXT, used TEXT, result TEXT, recurring_id TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS subtasks(
  id TEXT PRIMARY KEY, job_id TEXT, key TEXT, type TEXT, title TEXT, body TEXT, deps TEXT, gates TEXT,
  gate_of TEXT, status TEXT, assigned_agent TEXT, claimed_by TEXT, lease_expires REAL, attempts INTEGER DEFAULT 0,
  max_attempts INTEGER DEFAULT 3, budget TEXT, result TEXT, verdict TEXT, priority INTEGER DEFAULT 0,
  excluded_agents TEXT DEFAULT '[]', started_at REAL, created_at REAL, updated_at REAL);
CREATE INDEX IF NOT EXISTS subtasks_job ON subtasks(job_id, status);
CREATE TABLE IF NOT EXISTS leftovers(
  id TEXT PRIMARY KEY, text TEXT, task_type TEXT, priority INTEGER, project_id TEXT, source_id TEXT,
  recurring_id TEXT, status TEXT, carried_to TEXT, created_by TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS recurring(
  id TEXT PRIMARY KEY, name TEXT UNIQUE, cron TEXT, task TEXT, pattern TEXT, project_id TEXT, budget TEXT,
  created_by TEXT, enabled INTEGER DEFAULT 1, last_run REAL, next_run REAL, last_job TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS preferences(
  id TEXT PRIMARY KEY, statement TEXT, norm TEXT, category TEXT, project_id TEXT, task_type TEXT,
  confidence REAL, source TEXT, evidence TEXT, status TEXT, observations INTEGER DEFAULT 1,
  created_at REAL, last_confirmed REAL, updated_at REAL);
CREATE INDEX IF NOT EXISTS prefs_norm ON preferences(norm);
"""


def migrate(db):
    db.executescript(SCHEMA_V2)
    cols = {r[1] for r in db.execute("PRAGMA table_info(agents)")}
    if "profile" not in cols:
        db.execute("ALTER TABLE agents ADD COLUMN profile TEXT")
    if "platform" not in cols:
        db.execute("ALTER TABLE agents ADD COLUMN platform TEXT")


SCHEMA_V2_USAGE = """
CREATE TABLE IF NOT EXISTS usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, agent_id TEXT, project_id TEXT, job_id TEXT, subtask_id TEXT,
  model TEXT, tokens_in INTEGER, tokens_out INTEGER, cost REAL, seconds REAL, note TEXT);
CREATE INDEX IF NOT EXISTS usage_ts ON usage(ts);
"""
_orig_migrate = migrate


def migrate(db):  # noqa: F811
    _orig_migrate(db)
    db.executescript(SCHEMA_V2_USAGE)


SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS kb_state(bucket TEXT PRIMARY KEY, dirty INTEGER, dirtied_at REAL, built_at REAL);
CREATE TABLE IF NOT EXISTS kb_pages(bucket TEXT, section TEXT, title TEXT, content TEXT, citations TEXT,
  updated_at REAL, PRIMARY KEY(bucket, section));
"""
_v2_migrate = migrate


def migrate(db):  # noqa: F811
    _v2_migrate(db)
    db.executescript(SCHEMA_V3)
