"""v0.38 Canon on the hook client: the state cache a detached pull keeps, the session start that puts Canon first from
it, the per-prompt change lines and product cards, the start pack retried after a missed start, and the Canon core
block in every always-loaded instruction file. Everything fails open."""
import json
import os
import subprocess
import sys
import time

import pytest
from conftest import make_store

from commons_memory import canon as C
from commons_memory import canoncache as K
from commons_memory import capture as CP
from commons_memory import instructions as I
from commons_memory.capture import Capture, run_hook

OLD = ["$9 up to 10 people", "$19 up to 20 people"]
NEW = ["$19 up to 10 people", "$39 up to 20 people"]
REPO = "https://github.com/Neoo-Blue/santawishes"


def seed(s):
    s.record_decision("Seed Canon", "owner approved the seed", sets=[
        {"subject": "company", "predicate": "standing_rules", "value": ["No em dashes", "Ship without asking"]},
        {"subject": "product:santa", "predicate": "offer", "value": "A call from Santa"},
        {"subject": "product:santa", "predicate": "name", "value": "Santa Wishes"},
        {"subject": "work:santa", "predicate": "repos", "value": [REPO]},
        {"subject": "product:secret", "predicate": "prices", "value": OLD},
        {"subject": "product:secret", "predicate": "name", "value": "Secret Santa Hotline"},
        {"subject": "product:poise", "predicate": "platforms", "value": ["Windows", "Mac"]},
        {"subject": "product:poise", "predicate": "name_zh", "value": "泰然"},
        {"subject": "product:toastline", "predicate": "offer", "value": "Toasts, recorded"}])


def git(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c",
                           "init.defaultBranch=main", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout


def behind_checkout(tmp_path, remote=REPO + ".git", n=2):
    """A clone n commits behind its origin/main, whose origin URL is `remote` (no network: the refs are local)."""
    o, c = tmp_path / "origin", tmp_path / "checkout"
    o.mkdir()
    git(o, "init", "-q")
    for i in range(n + 1):
        (o / "f").write_text(str(i))
        git(o, "add", "f")
        git(o, "commit", "-qm", f"c{i}")
    git(tmp_path, "clone", "-q", str(o), str(c))
    git(c, "reset", "-q", "--hard", f"HEAD~{n}")
    git(c, "remote", "set-url", "origin", remote)
    return str(c)


def ctx(out):
    j = json.loads(out)
    return j["hookSpecificOutput"]["additionalContext"], j.get("systemMessage")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONS_HOME", str(tmp_path / "ch"))
    monkeypatch.setenv("COMMONS_AGENT", "claude")
    monkeypatch.delenv("COMMONS_URL", raising=False)
    monkeypatch.delenv("COMMONS_HOOK_CANON", raising=False)
    monkeypatch.delenv("COMMONS_HOOK_GIT", raising=False)
    s = make_store(tmp_path)
    seed(s)
    return s


def test_pull_writes_the_cache_and_a_failed_pull_keeps_it(env, tmp_path, monkeypatch):
    s = env
    c = K.pull(s)
    j = json.loads((K.home() / "canon.json").read_text())
    assert j["ok"] is True and j["error"] == "" and j["fetched_at"] and j == c
    assert set(j) >= {"core", "products", "card", "changes", "open_conflicts", "generated_at"}
    assert j["products"]["secret"]["product"]["prices"] == OLD
    md = (K.home() / "canon.md").read_text()
    head = md.split("\n\n")[0].splitlines()
    assert head[0] == f"CANON (current truth; precedence: {C.PRECEDENCE})" and len(head) <= 20
    assert md.count(C.PRECEDENCE) == 1 and head[1].startswith("COMPANY CORE") and "  - No em dashes" in head
    assert [b.split(" ", 1)[0] for b in md.strip().split("\n\n")[1:]] == ["poise", "santa", "secret", "toastline"]
    assert "- prices: $9 up to 10 people; $19 up to 20 people" in md
    # through the hub: one canon() call over MCP with the agent key
    monkeypatch.setenv("COMMONS_URL", "http://hub.test:8770")
    monkeypatch.setenv("COMMONS_API_KEY", "cmk_secret_test_key")
    from commons_memory.worker import McpClient
    calls = []
    monkeypatch.setattr(McpClient, "_call", lambda self, tool, args: calls.append((self.url, self.key, tool)) or
                        json.dumps(C.canon(s)))
    assert K.pull()["ok"] and calls == [("http://hub.test:8770/mcp", "cmk_secret_test_key", "canon")]
    # a failed pull keeps the old cache (canon.md too) and says why, never with the key in it
    old, when = (K.home() / "canon.md").read_text(), K.read()["fetched_at"]

    def down(self, tool, args):  # the MCP client's error comes wrapped in exception groups
        group = RuntimeError("unhandled errors in a TaskGroup (1 sub-exception)")
        group.exceptions = [ConnectionError("refused, key cmk_secret_test_key")]
        raise group
    monkeypatch.setattr(McpClient, "_call", down)
    sess = K.home() / "sessions"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "gone.json").write_text("{}")
    os.utime(sess / "gone.json", (time.time() - 15 * 86400,) * 2)
    bad = K.pull()
    assert bad["ok"] is False and bad["error"] == "ConnectionError: refused, key [key]"
    assert bad["products"]["secret"]["product"]["prices"] == OLD and bad["fetched_at"] == when
    assert (K.home() / "canon.md").read_text() == old and not (sess / "gone.json").exists()
    # no cache at all: ok=false with no data, and the hook says so
    (K.home() / "canon.json").unlink()
    assert K.pull() == {"fetched_at": None, "ok": False, "error": "ConnectionError: refused, key [key]"}
    assert "there is no Canon here yet" in K.status(K.read())


def test_cli_pull_and_detached_spawn(env, tmp_path, monkeypatch):
    from commons_memory.cli import main
    db = tmp_path / "cli.db"
    seed(make_store(tmp_path, "cli.db"))
    assert main(["--db", str(db), "canon", "pull"]) == 0
    assert K.read()["products"]["santa"]["work"]["repos"] == [REPO]
    seen = {}
    monkeypatch.setattr(K.subprocess, "Popen", lambda cmd, **kw: seen.update(cmd=cmd, **kw))
    K.spawn()
    safe = ["-P"] if sys.version_info >= (3, 11) else []  # never the session's project on sys.path: it may shadow
    assert seen["cmd"] == [sys.executable, *safe, "-m", "commons_memory", "canon", "pull"]
    assert seen["cwd"] == os.path.expanduser("~")
    assert seen["start_new_session"] and seen["stdout"] == seen["stderr"] == subprocess.DEVNULL
    assert seen["env"]["COMMONS_CALL_TIMEOUT"] == "30"
    assert K.due(60) and not K.due(60) and not K.due(300)  # one pull a minute at session starts, 5 min on prompts
    os.utime(K.home() / "canon.pull", (time.time() - 120,) * 2)
    assert not K.due(300) and K.due(60)


def test_session_start_puts_canon_first_from_the_cache(env, tmp_path, monkeypatch):
    s = env
    aid = s.ensure_agent("claude")
    s.add_item("memory", "Santa costs $9", subject="product:secret", predicate="prices", value="$9", agent_id=aid)
    s.add_item("memory", "Project uses pnpm, not npm", project="checkout")
    co = behind_checkout(tmp_path)
    out = run_hook("claude-code", "SessionStart", json.dumps({"session_id": "s1", "cwd": co, "source": "startup"}),
                   store_factory=lambda: s)
    text, notice = ctx(out)
    assert notice is None and text.startswith(f"CANON (current truth; precedence: {C.PRECEDENCE})")
    canon, _, pack = text.partition("\n\n# Commons memory (checkout)")
    assert "santa (product:santa" in canon and "- work.repos: " + REPO in canon and "secret (product:" not in canon
    assert "CANON CHANGES, last 7 days" in canon and "product:santa.offer: (none) -> A call from Santa" in canon
    lines = canon.split("CANON CHANGES, last 7 days")[1].splitlines()[1:]
    assert "product:toastline.offer" in canon  # every product's changes, this checkout's first
    assert [x.split(" ")[2].split(".")[0] for x in lines[:4]] == ["product:santa", "product:santa", "work:santa",
                                                                  "product:secret"]
    assert lines[8] == f"- and 1 earlier: changes(since='{C.before(C.changes(s)[0]['at'])}'); the next message shows them"
    assert "OPEN CONFLICTS in Commons: 1" in canon
    assert "GIT: this checkout is 2 commit(s) behind origin/main as of the last fetch" in canon
    assert "pnpm" in pack and "FACTS" not in pack  # the hub's pack follows, without Canon a second time
    st = K.session("s1")
    assert st["cards"] == ["santa"] and st["canon"] is True and st["seen_until"] > 0 and st["start_pending"] is False
    # every source (compact, resume, clear) gets it again, from the cache
    again, _ = ctx(run_hook("claude-code", "SessionStart", json.dumps({"session_id": "s1", "cwd": co, "source": "compact"}),
                            store_factory=lambda: s))
    assert again.startswith("CANON (") and "santa (product:santa" in again
    # outside any repo: the core, every change and the conflicts, no card, no git line
    home, _ = ctx(run_hook("claude-code", "SessionStart", json.dumps({"session_id": "s2", "cwd": str(tmp_path)}),
                           store_factory=lambda: s))
    assert "COMPANY CORE" in home and "product:toastline.offer" in home and "(product:santa" not in home
    assert "GIT:" not in home


@pytest.mark.parametrize("state,says", [("stale", "Commons Canon cache is 7 h old"), ("failed", "the last pull failed"),
                                        ("missing", "Commons Canon cache is missing")])
def test_a_missing_stale_or_failing_cache_is_said_in_one_line(env, tmp_path, monkeypatch, state, says):
    K.pull(env)
    c = K.read()
    if state == "stale":
        c["fetched_at"] = C.iso(time.time() - 7 * 3600)
    elif state == "failed":
        c |= {"ok": False, "error": "ConnectError: All connection attempts failed"}
    (K.home() / "canon.json").write_text(json.dumps(c))
    if state == "missing":
        (K.home() / "canon.json").unlink()
    monkeypatch.setenv("COMMONS_URL", "http://127.0.0.1:9")  # the hub is down too
    monkeypatch.setattr(K, "spawn", lambda: None)
    t = time.time()
    text, notice = ctx(run_hook("claude-code", "SessionStart", json.dumps({"session_id": "x", "cwd": str(tmp_path)})))
    assert time.time() - t < 2 and says in notice and text.splitlines()[0] == notice and "\n" not in notice
    assert ("COMPANY CORE" in text) is (state != "missing")  # the old cache is still shown, with the warning


def test_prompts_add_new_changes_once_and_named_products_once(env, tmp_path, monkeypatch):
    s = env
    co = behind_checkout(tmp_path)
    run_hook("claude-code", "SessionStart", json.dumps({"session_id": "p", "cwd": co}), store_factory=lambda: s)

    def prompt(text, sid="p", harness="claude-code", hevent="UserPromptSubmit"):
        out = run_hook(harness, hevent, json.dumps({"session_id": sid, "cwd": co, "prompt": text}),
                       store_factory=lambda: s)
        return ctx(out)[0] if out else ""
    held = prompt("ok")  # the start showed the newest 8 of 9: the one it held back comes now, then nothing
    assert "company.standing_rules: (none) -> No em dashes" in held and "(product:" not in held
    assert "CANON CHANGES" not in prompt("ok")
    d = s.record_decision("Hotline prices", "owner: 'make it 19 and 39'",
                          sets=[{"subject": "product:secret", "predicate": "prices", "value": NEW}])
    monkeypatch.setattr(K, "PULL_PROMPT_S", 0)  # the next prompt's pull (in-process on a local database)
    first = prompt("How is the Secret Santa Hotline selling?")
    assert "CANON CHANGES not yet shown in this session" in first and f"product:secret.prices: {'; '.join(OLD)} -> " \
        f"{'; '.join(NEW)} (by ?, {d['id']})" in first
    assert "secret (product:secret" in first and "santa (product:santa" not in first  # "santa" is inside the longer name
    second = prompt("How is the Secret Santa Hotline selling?")
    assert "CANON CHANGES" not in second and "(product:secret" not in second  # each change and card once
    # Chinese prompts: no spaces, no word gate
    zh = prompt("泰然的价格是多少")
    assert "poise (product:poise" in zh and "- platforms: Windows; Mac" in zh
    s.record_decision("Poise on Linux", "owner", sets=[{"subject": "product:poise", "predicate": "platforms",
                                                        "value": ["Windows", "Mac", "Linux"]}])
    zh2 = prompt("泰然现在支持哪些平台")
    assert "product:poise.platforms: Windows; Mac -> Windows; Mac; Linux" in zh2 and "(product:poise" not in zh2
    # a background task's report relayed as a turn names products without asking for their cards
    assert "(product:toastline" not in prompt("<task-notification> the Toastline agent finished")
    assert "toastline (product:toastline" in prompt("Toastline的价格")
    # Cursor shows hook context only at session start: its prompts move no watermark
    s.record_decision("Toastline offer", "owner", sets=[{"subject": "product:toastline", "predicate": "offer",
                                                         "value": "Toasts and rooms"}])
    before = K.session("p")
    assert prompt("anything new?", harness="cursor", hevent="beforeSubmitPrompt") == "" and K.session("p") == before
    # a session the hook never saw (or whose start was cut off) gets the session start's Canon with its first prompt
    fresh = prompt("hello there, what changed this week", sid="never-started")
    assert fresh.startswith("CANON (current truth") and "santa (product:santa" in fresh
    assert "CANON CHANGES, last 7 days" in fresh and "- and 4 earlier: changes(since=" in fresh
    assert K.session("never-started")["seen_until"] > 0


def test_names_and_watermark_edges():
    c = {"products": {"santa": {"product": {"name": "Santa Wishes"}}, "secret": {"product": {"name": "Secret Santa Hotline"}},
                      "cooli": {"product": {"names": ["Cooli AI"]}}, "journal": {}, "poise": {"work": {"aliases": {"zh": "泰然"}}},
                      "toastline": {}, "dailycall": {"product": {"name": "The Daily Call"}}}}
    assert K.mentioned(c, "Secret Santa Hotline prices, then santa wishes copy") == ["secret", "santa"]
    assert K.mentioned(c, "deploy the Cooli AI site and the journalctl logs, 泰然!") == ["cooli", "poise"]
    assert K.mentioned(c, "no product here") == [] and K.mentioned(c, "toastline prices") == ["toastline"]
    # everyday words name a product only through its name facts; no name shortened; _ and - are part of a word
    for text in ("the santa site", "rotate the GITHUB_WEBHOOK_SECRET", "add the client-secret", "write a journal entry",
                 "keep your poise", "deploy cooli.ai", "what did the daily standup say", "deploy toastline-dev",
                 "the toastline_db"):
        assert K.mentioned(c, text) == [], text
    # a long paste: only its first 16 KB is read, and each name once
    paste = "the toastline call log\n" * 20000
    t = time.time()
    assert K.mentioned(c, paste) == ["toastline"] and time.time() - t < 0.5
    assert K.mentioned(c, "x" * K.SCAN + " toastline") == []
    ch = [{"at": "2026-11-08T10:00:00Z", "subject": "company", "predicate": "focus", "new": ["santa"]}]
    st = {}
    c1 = {"changes": ch, "generated_at": "2026-11-08T10:00:00Z"}
    assert K.unseen(c1, st) == ch
    K.mark(c1, st, ch)
    late = ch + [{"at": "2026-11-08T10:00:00Z", "subject": "company", "predicate": "agents", "new": "x"}]
    # a change stamped the same second as the last pull is still delivered, and the first is not delivered again
    c2 = {"changes": late, "generated_at": "2026-11-08T10:00:01Z"}
    assert K.unseen(c2, st) == late[1:]
    K.mark(c2, st, late[1:])
    assert K.unseen(c2, st) == []
    # one that commits after a pull read Canon, stamped seconds before it, still arrives with the next pull
    lagged = late + [{"at": "2026-11-08T09:59:50Z", "subject": "company", "predicate": "focus", "new": ["poise"]}]
    assert K.unseen({"changes": lagged, "generated_at": "2026-11-08T10:00:05Z"}, st) == lagged[2:]
    # a change that was not shown keeps the watermark before it
    st2, c3 = {}, {"changes": late, "generated_at": "2026-11-08T10:05:00Z"}
    K.mark(c3, st2, late[1:])
    assert K.unseen(c3, st2) == late[:1]


def test_a_start_the_hub_misses_is_retried_with_the_next_prompt(env, tmp_path, monkeypatch):
    s = env
    K.pull(s)
    monkeypatch.setenv("COMMONS_URL", "http://hub.test")
    spawned, sent = [], []
    monkeypatch.setattr(K, "spawn", lambda: spawned.append(1))

    def hub(url, body, timeout):
        sent.append(body)
        if body["event"] == "session_start":
            raise TimeoutError("the hub is slow")
        return {"ok": True, "context": "LATE PACK" if body["payload"].get("start_pack") else "recall"}
    monkeypatch.setattr(CP, "_send", hub)
    t = time.time()
    text, _ = ctx(run_hook("claude-code", "SessionStart", json.dumps({"session_id": "m", "cwd": str(tmp_path)})))
    assert time.time() - t < 1 and text.startswith("CANON (") and spawned == [1]
    assert sent[0]["payload"]["canon_local"] is True and K.session("m")["start_pending"] is True
    text, _ = ctx(run_hook("claude-code", "UserPromptSubmit", json.dumps({"session_id": "m", "prompt": "continue"})))
    assert sent[1]["payload"]["start_pack"] is True and sent[1]["payload"]["canon_local"] is True
    assert text.endswith("\n\nLATE PACK") and K.session("m")["start_pending"] is False
    assert ctx(run_hook("claude-code", "UserPromptSubmit", json.dumps({"session_id": "m", "prompt": "next"})))[0] == "recall"
    assert "start_pack" not in sent[2]["payload"]
    # the hub's side: the late pack leads the prompt's context, with FACTS only when the client had no Canon
    aid = s.ensure_agent("claude")
    pl = {"project": "x", "session_id": "h", "prompt": "please carry on with the hotline work", "start_pack": True}
    with_facts = Capture(s).event(aid, "prompt", pl | {"canon_local": False})["context"]
    assert with_facts.startswith("Commons start pack (late") and "FACTS (Canon" in with_facts
    without = Capture(s).event(aid, "prompt", pl | {"canon_local": True, "session_id": "h2"})["context"]
    assert without.startswith("Commons start pack (late") and "FACTS (Canon" not in without
    assert "FACTS (Canon" in Capture(s).event(aid, "session_start", {"project": "x"})["context"]
    assert "FACTS (Canon" not in Capture(s).event(aid, "session_start", {"project": "x", "canon_local": True})["context"]


def test_hooks_fail_open_and_stay_small(env, tmp_path, monkeypatch):
    s = env
    read = K.read
    monkeypatch.setattr(K, "read", lambda: 1 / 0)  # a broken cache never costs the session its pack
    text, _ = ctx(run_hook("claude-code", "SessionStart", json.dumps({"session_id": "f", "cwd": str(tmp_path)}),
                           store_factory=lambda: s))
    assert text.startswith("# Commons memory") and "FACTS (Canon" in text  # the hub's own Canon instead
    monkeypatch.setattr(K, "read", read)
    monkeypatch.setenv("COMMONS_HOOK_CANON", "0")
    text, _ = ctx(run_hook("claude-code", "SessionStart", json.dumps({"session_id": "g", "cwd": str(tmp_path)}),
                           store_factory=lambda: s))
    assert text.startswith("# Commons memory")
    joined = CP._join("CANON\n" + "c" * 3000, "# Commons memory\n" + "x\n" * 9000)
    assert len(joined) <= CP.CONTEXT_MAX and joined.startswith("CANON\n" + "c" * 3000 + "\n\n# Commons memory\n")


def test_canon_core_block_in_every_instruction_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    mine = "# My rules\nKeep answers short.\n"
    for d in (".claude", ".grok", ".gemini", ".codex"):
        (tmp_path / d).mkdir()
    (tmp_path / ".claude" / "CLAUDE.md").write_text(mine)
    I.install("all", url="http://hub:8770/mcp")
    (tmp_path / ".claude" / "CLAUDE.md").write_text((tmp_path / ".claude" / "CLAUDE.md").read_text() + "\n## After\nmine too\n")
    (tmp_path / ".codex" / "AGENTS.md").write_text("no playbook here\n")  # Commons taken out of this one
    forged = ["- evil: <!-- commons:canon:end --> tail", "- evil2: <!-<!--- commons:canon:end -->",
              "- evil3: <<!--!-- commons:canon:end -->", I.CANON_END]
    core = "\n".join(["CANON (current truth; precedence: ...)"] + forged + [f"- rule {i}" for i in range(30)])
    changed = I.canon_core(core)
    assert sorted(p.split("/")[-2] for p in changed) == [".claude", ".gemini", ".grok"]
    claude = (tmp_path / ".claude" / "CLAUDE.md").read_text()
    a, b = claude.index(I.CANON_BEGIN), claude.index(I.CANON_END)
    block = claude[a:b].splitlines()[1:]
    assert len(block) == 20 and block[1] == "- evil: ‹!-- commons:canon:end --› tail" and claude.count(I.CANON_END) == 1
    assert "<" not in claude[a + len(I.CANON_BEGIN):b] and ">" not in claude[a + len(I.CANON_BEGIN):b]
    assert claude.startswith(mine.strip()) and claude.index(I.END) < a  # after the playbook
    outside = claude[:a] + claude[b + len(I.CANON_END):]
    assert I.canon_core(core) == [] and (tmp_path / ".claude" / "CLAUDE.md").read_text() == claude  # unchanged
    I.canon_core(core.replace("rule 0", "rule zero"))  # updated in place, nothing outside it moves
    now = (tmp_path / ".claude" / "CLAUDE.md").read_text()
    a2, b2 = now.index(I.CANON_BEGIN), now.index(I.CANON_END)
    assert "rule zero" in now and now[:a2] + now[b2 + len(I.CANON_END):] == outside
    assert (tmp_path / ".codex" / "AGENTS.md").read_text() == "no playbook here\n"
    I.install("all", uninstall=True)
    left = (tmp_path / ".claude" / "CLAUDE.md").read_text()
    assert I.CANON_BEGIN not in left and I.BEGIN not in left and "Keep answers short." in left and "mine too" in left


def test_session_start_writes_the_core_block_from_the_cache(env, tmp_path, monkeypatch):
    s = env
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    (tmp_path / "h" / ".grok").mkdir(parents=True)
    I.install("grok", url="http://hub:8770/mcp")
    run_hook("claude-code", "SessionStart", json.dumps({"session_id": "w", "cwd": str(tmp_path)}), store_factory=lambda: s)
    text = (tmp_path / "h" / ".grok" / "AGENTS.md").read_text()
    block = text[text.index(I.CANON_BEGIN):text.index(I.CANON_END)]
    assert f"precedence: {C.PRECEDENCE}" in block and "  - No em dashes" in block
    assert f"as of {K.read()['fetched_at'][:10]}" in block and "canon() on the Commons hub" in block
    assert "Santa Wishes" not in block  # the owner's core only: cards stay in the cache and the session context


def test_no_canon_yet_still_counts_conflicts_once(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONS_HOME", str(tmp_path / "ch"))
    monkeypatch.delenv("COMMONS_URL", raising=False)
    s = make_store(tmp_path)
    s.add_item("fact", "Alice works at Acme", subject="alice", predicate="employer", value="Acme")
    s.add_item("memory", "Alice works at Globex", subject="alice", predicate="employer", value="Globex")
    text, notice = ctx(run_hook("claude-code", "SessionStart", json.dumps({"session_id": "n", "cwd": str(tmp_path)}),
                                store_factory=lambda: s))
    assert not K.has_canon(K.read()) and notice is None and "COMPANY CORE" not in text
    assert text.count("OPEN CONFLICTS") == 1 and text.startswith("OPEN CONFLICTS in Commons: 1")


def test_a_late_pack_is_asked_for_once_with_the_time_left(env, tmp_path, monkeypatch):
    K.pull(env)
    monkeypatch.setenv("COMMONS_URL", "http://hub.test")
    monkeypatch.setattr(K, "spawn", lambda: None)
    sent = []

    def hub(url, body, timeout):  # a hub too slow to build a pack: every request for one times out
        sent.append((body["event"], bool(body["payload"].get("start_pack")), timeout))
        if body["event"] == "session_start" or body["payload"].get("start_pack"):
            raise TimeoutError("slow")
        return {"ok": True, "context": "recall for you"}
    monkeypatch.setattr(CP, "_send", hub)
    monkeypatch.setenv("COMMONS_HOOK_TIMEOUT", "10")
    import commons_memory.setup as SU  # a note from the MCP check is no answer from the hub
    monkeypatch.setattr(SU, "ensure_mcp", lambda harness, url: "Commons was registered again as your MCP server.")
    run_hook("claude-code", "SessionStart", json.dumps({"session_id": "lp", "cwd": str(tmp_path)}))
    assert K.session("lp")["start_pending"] is True
    assert sent[0][0] == "session_start" and sent[0][2] <= CP.QUEUE_HOOK_BUDGET_S  # a start waits for what is left
    monkeypatch.setenv("COMMONS_HOOK_TIMEOUT", "2")
    run_hook("claude-code", "UserPromptSubmit", json.dumps({"session_id": "lp", "prompt": "carry on please"}))
    assert sent[1][1] and sent[1][2] > 2  # the late pack gets the rest of the budget, once
    assert K.session("lp")["start_pending"] is False
    text, _ = ctx(run_hook("claude-code", "UserPromptSubmit", json.dumps({"session_id": "lp", "prompt": "and then?"})))
    assert not sent[2][1] and text.endswith("recall for you")


@pytest.mark.parametrize("state", ["stale", "failed", "old_hub"])
def test_the_hub_keeps_its_facts_unless_the_cache_is_fresh(env, tmp_path, monkeypatch, state):
    K.pull(env)
    c = K.read()
    c |= {"stale": {"fetched_at": C.iso(time.time() - 7 * 3600)}, "failed": {"ok": False, "error": "refused"},
          "old_hub": {"ok": False, "old_hub": True, "error": "older"}}[state]
    (K.home() / "canon.json").write_text(json.dumps(c))
    monkeypatch.setenv("COMMONS_URL", "http://hub.test")
    monkeypatch.setattr(K, "spawn", lambda: None)
    sent = []
    monkeypatch.setattr(CP, "_send", lambda url, body, timeout: sent.append(body) or {"ok": True, "context": "pack"})
    out = run_hook("claude-code", "SessionStart", json.dumps({"session_id": "f" + state, "cwd": str(tmp_path)}))
    assert sent[0]["payload"]["canon_local"] is False and K.session("f" + state)["canon"] is False
    assert (json.loads(out).get("systemMessage") is None) is (state == "old_hub")  # an old hub is no news


def test_an_old_hub_is_a_quiet_state(env, monkeypatch):
    from commons_memory.worker import McpClient
    monkeypatch.setenv("COMMONS_URL", "http://hub.test")

    def old(self, tool, args):
        raise RuntimeError("Unknown tool: canon")
    monkeypatch.setattr(McpClient, "_call", old)
    c = K.pull()
    assert c["old_hub"] and c["ok"] is False and "older than v0.38" in c["error"]
    assert K.status(c) == "" and not K.fresh(c) and K.block(c) is None


def test_the_session_is_saved_only_once_the_output_exists_and_a_bad_reply_is_no_reply(env, tmp_path, monkeypatch):
    K.pull(env)
    monkeypatch.setenv("COMMONS_URL", "http://hub.test")
    monkeypatch.setattr(K, "spawn", lambda: None)

    def killed(*a, **k):  # the harness kills the hook while it waits for the hub
        raise SystemExit(1)
    monkeypatch.setattr(CP, "_post", killed)
    with pytest.raises(SystemExit):
        run_hook("claude-code", "SessionStart", json.dumps({"session_id": "k", "cwd": str(tmp_path)}))
    assert K.session("k") == {}  # nothing claims the start's Canon was shown
    for reply in (["not", "a", "dict"], {"context": 5, "skills": "x"}, "text"):
        monkeypatch.setattr(CP, "_post", lambda *a, r=reply, **k: r)
        text, _ = ctx(run_hook("claude-code", "SessionStart", json.dumps({"session_id": "b", "cwd": str(tmp_path)})))
        assert text.startswith("CANON (") and K.session("b")["cards"] == []


def test_the_pull_starts_even_when_the_cache_cannot_be_read(env, tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONS_URL", "http://hub.test")
    spawned = []
    monkeypatch.setattr(K, "spawn", lambda: spawned.append(1))
    monkeypatch.setattr(K, "read", lambda: 1 / 0)
    monkeypatch.setattr(CP, "_send", lambda url, body, timeout: {"ok": True, "context": "pack"})
    assert ctx(run_hook("claude-code", "SessionStart", json.dumps({"session_id": "r", "cwd": str(tmp_path)})))[0] == "pack"
    assert spawned == [1]


def test_pull_redacts_the_key_checks_the_reply_and_bounds_the_cache(env, monkeypatch):
    from commons_memory.worker import McpClient
    key = "cmk_" + "k" * 40
    monkeypatch.setenv("COMMONS_URL", "http://hub.test")
    monkeypatch.setenv("COMMONS_API_KEY", key)

    def down(self, tool, args):  # the key straddles the 300th character
        raise ConnectionError("x" * 270 + " " + key + " tail")
    monkeypatch.setattr(McpClient, "_call", down)
    err = K.pull()["error"]
    assert "[key]" in err and "cmk_kkk" not in err and key[:20] not in json.dumps(K.read())
    good = C.canon(env)
    for bad in ({**good, "changes": [{"at": "not a time"}]}, {**good, "generated_at": None}, {**good, "changes": ["x"]}):
        monkeypatch.setattr(McpClient, "_call", lambda self, tool, args, b=bad: json.dumps(b))
        assert K.pull()["ok"] is False
    many = [{"at": C.iso(time.time() - 3600 + i), "subject": "company", "predicate": f"p{i}", "old": None, "new": i,
             "by": "x", "decision": None} for i in range(K.CHANGES_MAX + 20)]
    monkeypatch.setattr(McpClient, "_call", lambda self, tool, args: json.dumps({**good, "changes": many}))
    c = K.pull()
    assert len(c["changes"]) == K.CHANGES_MAX and c["changes_cut"] == 20 and c["changes"][-1]["new"] == many[-1]["new"]
    text, _ = K.start(c, None, {})
    assert f"- and {K.CHANGES_MAX - 8 + 20} earlier: changes(since='7d')" in text
    (K.home() / "canon.json").write_text(json.dumps({**good, "pad": "x" * K.MAX_BYTES}))
    assert K.read() is None  # too big to be a cache: corrupt
    monkeypatch.delenv("COMMONS_URL")
    monkeypatch.delenv("COMMONS_API_KEY")
    assert K.pull(env)["ok"] is True  # a local database needs no key


def test_the_hook_never_loads_the_mcp_server(env, tmp_path):
    K.pull(env)
    (K.home() / "canon.pull").touch()  # a pull just ran: the hook in the child starts none
    code = ("import json, sys; from commons_memory.capture import run_hook; "
            f"run_hook('claude-code', 'SessionStart', json.dumps({{'session_id': 'i', 'cwd': {str(tmp_path)!r}}})); "
            "run_hook('claude-code', 'UserPromptSubmit', json.dumps({'session_id': 'i', 'prompt': 'hello there friend'})); "
            "print(sorted(m for m in sys.modules if m in ('commons_memory.server', 'mcp.server.fastmcp')))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                       env={**os.environ, "COMMONS_URL": "http://127.0.0.1:9", "COMMONS_HOOK_GIT": "0"})
    assert r.stdout.strip().splitlines()[-1] == "[]", r.stderr[-2000:]


def test_canon_stays_under_its_cap_with_one_card_at_start(env, tmp_path, monkeypatch):
    s = env
    long = "word " * 400
    sets = [{"subject": "company", "predicate": f"note{i}", "value": long} for i in range(25)]
    sets += [{"subject": "work:santa", "predicate": "repos", "value": [REPO]},
             {"subject": "work:toastline", "predicate": "repos", "value": [REPO]},
             {"subject": "product:santa", "predicate": "features", "value": [long] * 30},
             {"subject": "product:toastline", "predicate": "features", "value": [long] * 30}]
    s.record_decision("Big Canon", "owner", sets=sets)
    c = K.pull(s)
    assert all(len(x) <= 240 for x in K.core(c).splitlines()[1:] if not x.startswith(K.FIXED))
    text, _ = K.start(c, None, {})
    assert len(text) <= K.CANON_MAX
    co = behind_checkout(tmp_path)
    st = {}
    text, _ = K.start(c, co, st)
    assert len(text) <= K.CANON_MAX and "GIT: this checkout" in text and "CANON CHANGES, last 7 days" in text
    assert text.count("(product:santa = what customers") + text.count("(product:toastline = what") <= 1
    assert st["cards"] in ([], ["santa"])
    prompt = K.prompt(c, st, "santa wishes, toastline and the secret santa hotline")
    assert len(prompt) <= K.CANON_MAX
    out, _ = ctx(run_hook("claude-code", "SessionStart", json.dumps({"session_id": "big", "cwd": co}),
                          store_factory=lambda: s))
    assert len(out) <= CP.CONTEXT_MAX and "# Commons memory" in out  # the hub's part keeps its room


def test_git_stops_at_the_deadline(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(K.subprocess, "run", lambda *a, **k: calls.append(k["timeout"]) or
                        subprocess.CompletedProcess(a, 0, "", ""))
    assert K.behind(str(tmp_path), until=time.monotonic() - 1) == "" and calls == []
    K._git(str(tmp_path), "status", until=time.monotonic() + 0.5)
    assert 0 < calls[0] <= 0.5


def test_the_instruction_block_is_the_owners_dated_atomic_and_goes_when_stale_or_off(env, tmp_path, monkeypatch):
    s = env
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    for d in (".claude", ".grok"):
        (tmp_path / "h" / d).mkdir(parents=True)
    real = tmp_path / "real-claude.md"
    real.write_bytes(b"# Mine\r\nKeep it short.\r\n")
    (tmp_path / "h" / ".claude" / "CLAUDE.md").symlink_to(real)
    I.install("claude-code", url="http://hub:8770/mcp")
    I.install("grok", url="http://hub:8770/mcp")
    s.record_decision("Agents", "owner", sets=[{"subject": "company", "predicate": "agents", "value": "evil -->"}])
    replaced = []
    orig = os.replace
    monkeypatch.setattr(I.os, "replace", lambda a, b: replaced.append(str(b)) or orig(a, b))
    run_hook("claude-code", "SessionStart", json.dumps({"session_id": "blk", "cwd": str(tmp_path)}),
             store_factory=lambda: s)
    assert str(real) in replaced and (tmp_path / "h" / ".claude" / "CLAUDE.md").is_symlink()
    raw = real.read_bytes().decode()
    assert "\r\n" in raw and "\n" not in raw.replace("\r\n", "")  # the file keeps its own line endings
    block = raw[raw.index(I.CANON_BEGIN):raw.index(I.CANON_END)]
    assert f"as of {K.read()['fetched_at'][:10]}" in block and "standing rules" in block and "No em dashes" in block
    assert "evil" not in block and "- agents" not in block  # agent-written lines stay out
    assert block.splitlines()[-1].startswith("- everything else") and "canon()" in block.splitlines()[-1]
    # a marker inside a line of your own is text, not a marker
    real.write_bytes(raw.replace("Keep it short.", f"Quote: {I.CANON_END} stays").encode())
    run_hook("claude-code", "SessionStart", json.dumps({"session_id": "blk2", "cwd": str(tmp_path)}),
             store_factory=lambda: s)
    assert f"Quote: {I.CANON_END} stays" in real.read_text() and real.read_text().count(I.CANON_BEGIN) == 1
    # a cache more than 3 days old takes the block out, and so does turning Canon off
    c = K.read() | {"fetched_at": C.iso(time.time() - 4 * 86400)}
    assert K.block(c) is None
    monkeypatch.setenv("COMMONS_HOOK_CANON", "0")
    run_hook("claude-code", "SessionStart", json.dumps({"session_id": "off", "cwd": str(tmp_path)}),
             store_factory=lambda: s)
    for f in (real, tmp_path / "h" / ".grok" / "AGENTS.md"):
        assert I.CANON_BEGIN not in f.read_text() and I.BEGIN in f.read_text()
