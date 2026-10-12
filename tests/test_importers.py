"""v0.9 importers: claude-mem, Claude Code memory files, sesame (secrets never travel), idempotent re-runs."""
import json
import sqlite3

from commons_memory import importers as I


def _claude_mem_db(path):
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE observations(id INTEGER PRIMARY KEY, memory_session_id TEXT, project TEXT, text TEXT, type TEXT,
      title TEXT, subtitle TEXT, facts TEXT, narrative TEXT, concepts TEXT, files_read TEXT, files_modified TEXT,
      created_at_epoch INTEGER, merged_into_project TEXT);
    CREATE TABLE session_summaries(id INTEGER PRIMARY KEY, project TEXT, request TEXT, investigated TEXT, learned TEXT,
      completed TEXT, next_steps TEXT, notes TEXT, created_at_epoch INTEGER, merged_into_project TEXT);
    CREATE TABLE user_prompts(id INTEGER PRIMARY KEY, project TEXT, prompt_text TEXT, created_at_epoch INTEGER);""")
    db.execute("INSERT INTO observations VALUES(1,'s','warroom',NULL,'decision','Use PMTiles for counties',"
               "'Range-served tiles','We serve county boundaries as PMTiles.','[\"49k counties\"]','[\"pattern\"]',"
               "'[]','[\"deploy.sh\"]',1790000000000,NULL)")
    db.execute("INSERT INTO observations VALUES(2,'s','.claude_proxy_cwd',NULL,'bugfix','Fix fetch storm','',"
               "'An unstable dependency looped /api/events.','[]','[]','[]','[]',1790000100000,NULL)")
    db.execute("INSERT INTO session_summaries VALUES(1,'warroom','ship the county layer','tiles','gzip broke ranges',"
               "'layer live','purge cache',NULL,1790000200000,NULL)")
    db.execute("INSERT INTO user_prompts VALUES(1,'warroom','my key is sk-ant-api03-SECRETSECRETSECRETSECRET',1790000300000)")
    db.commit()


def test_claude_mem_memory_files_and_sesame_import(store, tmp_path):
    _claude_mem_db(tmp_path / "cm.db")
    items = I.collect("claude-mem", path=str(tmp_path / "cm.db"))
    assert len(items) == 3  # prompts are opt-in
    assert items[0]["kind"] == "decision" and items[0]["project"] == "warroom" and items[0]["created_at"] == 1790000000.0
    assert items[1]["project"] == "claude_proxy_cwd"  # leading dot dropped -> valid project name
    r = I.import_items(store, items)
    assert r["added"] == 3 and I.import_items(store, items)["skipped"] == 3  # idempotent
    hit = store.search("county boundaries PMTiles", project="warroom")["results"]
    assert hit and hit[0]["kind"] == "decision" and hit[0]["created_at"].startswith("2026-09")
    with_prompts = I.collect("claude-mem", path=str(tmp_path / "cm.db"), prompts=True)
    I.import_items(store, with_prompts)
    assert "SECRETSECRET" not in json.dumps([dict(x) for x in store.q("SELECT content FROM items")])  # redacted on write

    mem = tmp_path / "projects" / "-Users-me" / "memory"
    mem.mkdir(parents=True)
    (mem / "feedback_no_dashes.md").write_text("---\nname: no-dashes\ndescription: never use em dashes in replies\n"
                                               "metadata:\n  type: feedback\n---\n\nUse commas instead.\n")
    (mem / "MEMORY.md").write_text("- index")
    r = I.import_items(store, I.collect("claude-memory", root=str(tmp_path / "projects")))
    assert r["added"] == 1 and r["preferences"] == 1
    from commons_memory.prefs import Preferences
    assert "em dashes" in Preferences(store).get(max_tokens=400)["text"]

    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"services": {"mem0": {"label": "Mem0", "category": "ai", "status": "active",
        "urls": {"api": "https://mem0.example", "instances": [{"name": "lan", "url": "http://10.0.0.5:8888"}]},
        "auth": {"kind": "stored", "fields": [{"name": "api_key", "env": ["MEM0_KEY"], "secret": True, "ref": "mem0/api_key",
                                                "value": "SHOULD-NEVER-LEAK"},
                                               {"name": "url", "env": ["MEM0_URL"], "secret": False, "value": "http://10.0.0.5:8888"}]},
        "recipes": [{"name": "list", "run": "sesame run mem0 -- mem0ctl list"}], "gotchas": ["send a User-Agent"]}}}))
    items = I.collect("sesame", path=str(reg))
    txt = items[0]["content"]
    assert "env://MEM0_KEY" in txt and "SHOULD-NEVER-LEAK" not in txt and "http://10.0.0.5:8888" in txt
    assert "recipe list" in txt and "User-Agent" in txt
    assert I.import_items(store, items)["added"] == 1


def test_curated_import_with_ai_manager(store):
    """--curate: the curator role (mock provider here) drops noise, merges repeats, queues uncertain items for review,
    extracts preferences, and a re-run reviews nothing twice."""
    from commons_memory.curate import curate
    from commons_memory.providers import ProviderLayer
    from commons_memory.retro import RetroLearner
    pl = ProviderLayer(store)
    pl.add_provider("mock", "mock")
    pl.set_role("curator", "mock/curator-1")
    items = [{"src": "t:1", "title": "Build status: CI green", "content": "ci passed", "project": "web"},
             {"src": "t:2", "title": "Payments use Stripe webhooks", "content": "We decided on Stripe webhooks; retries are idempotent.", "project": "web"},
             {"src": "t:3", "title": "Payments use Stripe webhooks", "content": "Webhook secret rotates monthly.", "project": "web"},
             {"src": "t:4", "title": "Maybe switch to Postgres 18", "content": "Unclear if needed.", "project": "web"},
             {"src": "t:5", "title": "No em dashes", "content": "Never use em dashes in copy.", "project": None},
             {"src": "t:6", "title": "No em dashes", "content": "Never use em dashes in copy.", "project": None}]
    est = curate(store, items, dry_run=True)
    assert est["duplicates_removed"] == 1 and est["items"] == 5 and est["tokens_in"] > 0
    r = curate(store, items, workers=2)
    assert (r["kept"], r["queued"], r["dropped"], r["merged"], r["duplicates_removed"]) == (2, 1, 1, 1, 1)
    hit = store.search("Stripe webhooks retries", project="web")["results"]
    assert hit and hit[0]["kind"] == "decision"
    assert "rotates monthly" in store.expand(hit[0]["id"])["content"]  # the merged item's text was folded in
    from commons_memory.prefs import Preferences
    assert "em dashes" in Preferences(store).get(max_tokens=400)["text"]
    q = RetroLearner(store).queue()
    assert len(q) == 1 and "Postgres" in q[0]["text"]
    RetroLearner(store).decide([q[0]["id"]], True)
    assert store.search("Postgres 18")["results"]  # approving the queued item imports it
    again = curate(store, items, workers=2)
    assert again["batches"] == 0 and again["kept"] == 0  # nothing reviewed (or paid for) twice
