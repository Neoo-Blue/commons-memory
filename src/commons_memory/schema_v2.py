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


def migrate(db):  # noqa: F811
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


SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS skills(
  id TEXT PRIMARY KEY, name TEXT UNIQUE, description TEXT, tags TEXT, deps TEXT, author_agent TEXT,
  active_version INTEGER, forked_from TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS skill_versions(
  id TEXT PRIMARY KEY, skill_id TEXT, version INTEGER, content TEXT, files TEXT, author_agent TEXT, changelog TEXT,
  status TEXT, review TEXT, findings TEXT, created_at REAL, UNIQUE(skill_id, version));
CREATE TABLE IF NOT EXISTS skill_usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT, skill_id TEXT, version INTEGER, agent_id TEXT, model TEXT, subtask_id TEXT,
  success INTEGER, rating REAL, note TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS research_cache(
  key TEXT PRIMARY KEY, provider TEXT, query TEXT, results TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS providers(
  name TEXT PRIMARY KEY, kind TEXT, base_url TEXT, key_ref TEXT, enabled INTEGER DEFAULT 1, config TEXT,
  last_refresh REAL, last_error TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS models(
  id TEXT PRIMARY KEY, provider TEXT, model TEXT, context INTEGER, cost_in REAL, cost_out REAL, capabilities TEXT,
  speed REAL, first_seen REAL, last_seen REAL, is_new INTEGER DEFAULT 0, status TEXT DEFAULT 'available', meta TEXT);
CREATE TABLE IF NOT EXISTS agent_models(agent_id TEXT, model_id TEXT, PRIMARY KEY(agent_id, model_id));
CREATE TABLE IF NOT EXISTS model_scores(
  model_id TEXT, task_type TEXT, n INTEGER, score_sum REAL, cost REAL, updated_at REAL, PRIMARY KEY(model_id, task_type));
CREATE TABLE IF NOT EXISTS routing(
  task_type TEXT PRIMARY KEY, agent_id TEXT, model_id TEXT, reason TEXT, source TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS proposals(
  id TEXT PRIMARY KEY, task_type TEXT, from_model TEXT, to_model TEXT, agent_id TEXT, evidence TEXT, status TEXT,
  created_at REAL, decided_at REAL);
"""
_v3_migrate = migrate


def migrate(db):  # noqa: F811
    _v3_migrate(db)
    db.executescript(SCHEMA_V4)


SCHEMA_V4B = """
CREATE TABLE IF NOT EXISTS librarian_log(
  id TEXT PRIMARY KEY, run_id TEXT, ts REAL, action TEXT, target TEXT, before TEXT, after TEXT, actor TEXT,
  status TEXT, reversible INTEGER);
CREATE TABLE IF NOT EXISTS librarian_proposals(
  id TEXT PRIMARY KEY, kind TEXT, target TEXT, payload TEXT, reason TEXT, status TEXT, created_at REAL, decided_at REAL);
CREATE TABLE IF NOT EXISTS librarian_index(term TEXT, path TEXT, key TEXT, rid TEXT, label TEXT, kind TEXT, weight REAL);
CREATE INDEX IF NOT EXISTS lib_idx_term ON librarian_index(term);
CREATE TABLE IF NOT EXISTS node_summaries(key TEXT PRIMARY KEY, path TEXT, summary TEXT, source TEXT, ts REAL);
"""
_v4_migrate = migrate


def migrate(db):  # noqa: F811
    _v4_migrate(db)
    db.executescript(SCHEMA_V4B)


SCHEMA_V4C = """
CREATE TABLE IF NOT EXISTS mcp_servers(
  name TEXT PRIMARY KEY, transport TEXT, command TEXT, args TEXT, url TEXT, env_refs TEXT, header_refs TEXT,
  preset TEXT, enabled INTEGER DEFAULT 1, status TEXT, last_check REAL, last_error TEXT, restarts INTEGER DEFAULT 0,
  rate_per_min INTEGER DEFAULT 60, created_at REAL);
CREATE TABLE IF NOT EXISTS mcp_tools(
  server TEXT, name TEXT, description TEXT, schema TEXT, risk TEXT, updated_at REAL, PRIMARY KEY(server, name));
CREATE TABLE IF NOT EXISTS mcp_policies(
  id TEXT PRIMARY KEY, agent TEXT, server TEXT, tool TEXT, effect TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS mcp_calls(
  id TEXT PRIMARY KEY, ts REAL, agent_id TEXT, server TEXT, tool TEXT, args TEXT, status TEXT, ms INTEGER,
  cached INTEGER, size INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS mcp_approvals(
  id TEXT PRIMARY KEY, agent_id TEXT, server TEXT, tool TEXT, args TEXT, args_hash TEXT, status TEXT,
  created_at REAL, decided_at REAL);
CREATE TABLE IF NOT EXISTS mcp_cache(key TEXT PRIMARY KEY, result TEXT, ts REAL);
"""
_v4b_migrate = migrate


def migrate(db):  # noqa: F811
    _v4b_migrate(db)
    db.executescript(SCHEMA_V4C)


SCHEMA_V4D = """
CREATE TABLE IF NOT EXISTS skill_catalog(
  id TEXT PRIMARY KEY, repo TEXT, path TEXT, name TEXT, description TEXT, url TEXT, license TEXT, sha TEXT,
  stars INTEGER, forks INTEGER, pushed_at TEXT, kind TEXT, topics TEXT, first_seen REAL, last_seen REAL,
  imported_skill TEXT, imported_sha TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS skill_catalog_history(repo TEXT, ts REAL, stars INTEGER, forks INTEGER);
CREATE INDEX IF NOT EXISTS sch_repo ON skill_catalog_history(repo, ts);
"""
_v4c_migrate = migrate


def migrate(db):  # noqa: F811
    _v4c_migrate(db)
    db.executescript(SCHEMA_V4D)


SCHEMA_V4E = """
CREATE TABLE IF NOT EXISTS context_blobs(hash TEXT PRIMARY KEY, content TEXT, chars INTEGER, created_at REAL);
CREATE TABLE IF NOT EXISTS context_packs(
  id TEXT PRIMARY KEY, name TEXT, agent_id TEXT, project_id TEXT, parent_id TEXT, session_id TEXT, created_at REAL,
  gist TEXT, brief_hash TEXT, sections TEXT, refs TEXT, prefix_hash TEXT, stats TEXT);
CREATE INDEX IF NOT EXISTS cp_name ON context_packs(name, created_at);
CREATE TABLE IF NOT EXISTS context_loads(
  id INTEGER PRIMARY KEY AUTOINCREMENT, pack_id TEXT, agent_id TEXT, layer TEXT, model TEXT, tokens INTEGER,
  full_tokens INTEGER, ts REAL);
"""
_v4d_migrate = migrate


def migrate(db):  # noqa: F811
    _v4d_migrate(db)
    db.executescript(SCHEMA_V4E)
    cols = {r[1] for r in db.execute("PRAGMA table_info(usage)")}
    if "cached_tokens" not in cols:
        db.execute("ALTER TABLE usage ADD COLUMN cached_tokens INTEGER DEFAULT 0")


def _v5(db):
    from .auth import ACL_SCHEMA
    db.executescript(ACL_SCHEMA)
    from .v5schema import SCHEMA_V5, add_columns
    db.executescript(SCHEMA_V5)
    add_columns(db)
    from .retro import SCHEMA_V6
    db.executescript(SCHEMA_V6)
    from .messaging import SCHEMA_V7
    db.executescript(SCHEMA_V7)
    from .routing import SCHEMA as ROUTING_SCHEMA
    db.executescript(ROUTING_SCHEMA)
    from .drive import SCHEMA as DRIVE_SCHEMA
    db.executescript(DRIVE_SCHEMA)
    from .scheduler import SCHEMA as SCHED_SCHEMA
    from .scheduler import add_columns as sched_columns
    db.executescript(SCHED_SCHEMA)
    sched_columns(db)
    from .capture import SCHEMA as CAPTURE_SCHEMA
    db.executescript(CAPTURE_SCHEMA)
    try:
        from .facts import add_columns as fact_columns
    except ImportError:  # pragma: no cover
        fact_columns = None
    if fact_columns:
        fact_columns(db)
    from .tiers import add_columns as tier_columns
    tier_columns(db)
    from .board import migrate as board_migrate
    board_migrate(db)
    from .quota import migrate as quota_migrate
    quota_migrate(db)
    from .schedules import migrate as schedules_migrate
    schedules_migrate(db)
    from .work import migrate as work_migrate
    work_migrate(db)
    from .vault import migrate as vault_migrate
    vault_migrate(db)


_v4e_migrate = migrate


def migrate(db):  # noqa: F811
    _v4e_migrate(db)
    _v5(db)
