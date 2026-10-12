"""Fixture histories in each supported format (shapes taken from real exports/session files)."""
import json
import os
import sqlite3
import stat
import subprocess
from pathlib import Path

SECRET = "sk-ant-api03-" + "Q" * 40


def build(home: Path) -> dict:
    p = {}
    # Claude Code: ~/.claude/projects/<slug>/<session>.jsonl
    cc = home / ".claude/projects/-home-me-shop"
    cc.mkdir(parents=True)
    lines = [
        {"type": "user", "sessionId": "s1", "cwd": "/home/me/shop", "timestamp": "2026-03-01T10:00:00Z",
         "message": {"role": "user", "content": "Fix the checkout bug. I prefer small focused commits."}},
        {"type": "assistant", "sessionId": "s1", "cwd": "/home/me/shop", "timestamp": "2026-03-01T10:01:00Z",
         "message": {"role": "assistant", "model": "claude-sonnet-4-5", "content": [
             {"type": "text", "text": "We decided to use Stripe webhooks because polling missed refunds."},
             {"type": "tool_use", "name": "Bash", "input": {"command": f"export KEY={SECRET}"}}]}},
        {"type": "user", "sessionId": "s1", "cwd": "/home/me/shop", "timestamp": "2026-03-01T10:05:00Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
        {"type": "user", "sessionId": "s1", "cwd": "/home/me/shop", "timestamp": "2026-03-01T10:06:00Z",
         "message": {"role": "user", "content": f"thanks, that works. my key is {SECRET}"}},
        {"type": "assistant", "sessionId": "s1", "cwd": "/home/me/shop", "timestamp": "2026-03-01T10:07:00Z",
         "message": {"role": "assistant", "model": "claude-sonnet-4-5", "content": "Done.\nNext steps: add a retry queue for failed webhooks."}},
    ]
    (cc / "s1.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    p["claude_jsonl"] = cc / "s1.jsonl"
    # Codex rollout
    cx = home / ".codex/sessions/2026/03/02"
    cx.mkdir(parents=True)
    rows = [
        {"timestamp": "2026-03-02T09:00:00Z", "type": "session_meta", "payload": {"id": "c1", "cwd": "/home/me/api"}},
        {"timestamp": "2026-03-02T09:00:01Z", "type": "turn_context", "payload": {"cwd": "/home/me/api", "model": "gpt-5-codex"}},
        {"timestamp": "2026-03-02T09:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "<environment_context>cwd</environment_context>"}]}},
        {"timestamp": "2026-03-02T09:00:03Z", "type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "Please always run ruff before committing. Refactor the auth module."}]}},
        {"timestamp": "2026-03-02T09:00:04Z", "type": "response_item", "payload": {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "The service uses FastAPI with Postgres. TODO: migrate the session table."}]}},
        {"timestamp": "2026-03-02T09:00:05Z", "type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "that's wrong, it still fails"}]}},
    ]
    (cx / "rollout-2026-03-02T09-00-00-c1.jsonl").write_text("\n".join(json.dumps(x) for x in rows) + "\n")
    # Cursor state DB (new composer + bubble keys, old inline conversation, legacy aichat)
    cdb = home / ".config/Cursor/User/globalStorage"
    cdb.mkdir(parents=True)
    db = sqlite3.connect(cdb / "state.vscdb")
    db.execute("CREATE TABLE cursorDiskKV(key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    db.execute("CREATE TABLE ItemTable(key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    db.execute("INSERT INTO cursorDiskKV VALUES(?,?)", ("composerData:k1", json.dumps({
        "composerId": "k1", "name": "Dark mode", "createdAt": 1772400000000,
        "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}, {"bubbleId": "b2", "type": 2}]})))
    db.execute("INSERT INTO cursorDiskKV VALUES(?,?)", ("bubbleId:k1:b1", json.dumps({"type": 1, "text": "Never use inline styles in React components."})))
    db.execute("INSERT INTO cursorDiskKV VALUES(?,?)", ("bubbleId:k1:b2", json.dumps({"type": 2, "text": "Going with CSS modules since the repo already uses them."})))
    db.execute("INSERT INTO cursorDiskKV VALUES(?,?)", ("composerData:k2", json.dumps({
        "composerId": "k2", "conversation": [{"type": 1, "text": "How do I release a new version?"},
                                             {"type": 2, "text": "1. Bump the version\n2. Update the changelog\n3. Tag the commit\n4. Push the tag\n5. Create the GitHub release"}]})))
    db.execute("INSERT INTO ItemTable VALUES(?,?)", ("workbench.panel.aichat.view.aichat.chatdata", json.dumps(
        {"tabs": [{"tabId": "t1", "chatTitle": "old", "bubbles": [{"type": "user", "text": "We use pnpm, not npm."},
                                                                 {"type": "ai", "text": "Noted."}]}]})))
    db.commit()
    db.close()
    # ChatGPT export
    dl = home / "Downloads/chatgpt-export"
    dl.mkdir(parents=True)
    mapping = {
        "root": {"id": "root", "message": None, "parent": None, "children": ["u1"]},
        "u1": {"id": "u1", "parent": "root", "children": ["a1", "a1b"], "message": {"author": {"role": "user"}, "create_time": 1772000000,
               "content": {"content_type": "text", "parts": ["I like answers with code examples first."]}}},
        "a1b": {"id": "a1b", "parent": "u1", "children": [], "message": {"author": {"role": "assistant"},
                "content": {"parts": ["ABANDONED BRANCH should not appear"]}}},
        "a1": {"id": "a1", "parent": "u1", "children": [], "message": {"author": {"role": "assistant"}, "create_time": 1772000010,
               "metadata": {"model_slug": "gpt-4o"}, "content": {"content_type": "text", "parts": ["Sure."]}}}}
    (dl / "conversations.json").write_text(json.dumps([{"id": "g1", "title": "style", "create_time": 1772000000,
                                                         "current_node": "a1", "mapping": mapping}]))
    # Claude.ai export
    ca = home / "Downloads/claude-export"
    ca.mkdir(parents=True)
    (ca / "conversations.json").write_text(json.dumps([{"uuid": "ca1", "name": "infra", "created_at": "2026-02-01T00:00:00Z",
        "chat_messages": [{"sender": "human", "text": "We decided to host on Fly.io because of regions.", "created_at": "2026-02-01T00:00:00Z"},
                          {"sender": "assistant", "text": "", "content": [{"type": "text", "text": "Good choice."}]}]}]))
    # Gemini CLI
    gm = home / ".gemini/tmp/abc123"
    (gm / "chats").mkdir(parents=True)
    (gm / "chats/session-1.json").write_text(json.dumps({"sessionId": "gs1", "projectHash": "abc123", "messages": [
        {"type": "user", "content": "Avoid adding new dependencies without asking.", "timestamp": "2026-03-03T00:00:00Z"},
        {"type": "gemini", "content": "Understood.", "model": "gemini-2.5-pro"}]}))
    (gm / "logs.json").write_text(json.dumps([{"sessionId": "gs2", "messageId": 0, "type": "user",
                                                "message": "always write docstrings", "timestamp": "2026-03-04T00:00:00Z"}]))
    (gm / "checkpoint-x.json").write_text(json.dumps([{"role": "user", "parts": [{"text": "The app is deployed to Cloud Run."}]},
                                                       {"role": "model", "parts": [{"text": "ok"}]}]))
    # generic markdown transcript
    gen = home / "notes"
    gen.mkdir()
    (gen / "chat.md").write_text("## User\nWe'll use SQLite for the prototype because it needs zero setup.\n\n"
                                 "## Assistant\nSounds good.\n\n- TODO: write the migration script\n")
    p["generic"] = gen
    # memory files
    proj = home / "work/shop"
    (proj / ".cursor/rules").mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("# Rules\n- Use pytest for all tests\n- The API lives in src/api\n")
    (proj / "AGENTS.md").write_text("1. Prefer type hints everywhere\n")
    (proj / ".cursor/rules/style.mdc").write_text("---\ndescription: style\n---\n- Never commit .env files\n")
    (home / "mem0.json").write_text(json.dumps({"results": [{"memory": "User's favourite editor is Neovim", "created_at": "2026-01-01T00:00:00Z"}]}))
    p["memdir"], p["mem0"] = proj, home / "mem0.json"
    # git repo + mock gh
    repo = home / "repo"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL="a@x", GIT_COMMITTER_NAME="a", GIT_COMMITTER_EMAIL="a@x")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for i, msg in enumerate(["feat: add billing export", "Switch to argon2 for password hashing\n\nbecause bcrypt limits input to 72 bytes"]):
        (repo / f"f{i}").write_text(str(i))
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", msg], check=True, env=env)
    p["repo"], p["git_env"] = repo, env
    bin_ = home / "bin"
    bin_.mkdir()
    gh = bin_ / "gh"
    gh.write_text("#!/bin/sh\necho '[{\"number\":7,\"title\":\"Use Redis for rate limits\",\"body\":\"We decided to move rate "
                  "limiting to Redis because per-process buckets break with replicas.\",\"author\":{\"login\":\"a\"},"
                  "\"createdAt\":\"2026-03-05T00:00:00Z\",\"mergedAt\":null,\"state\":\"MERGED\"}]'\n")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    p["bin"] = bin_
    return p
