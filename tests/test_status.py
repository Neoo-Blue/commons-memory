import base64
import json
import os

from commons_memory import status as ST


def test_status_cards_follow_the_work_and_show_needs_you_first(store):
    from commons_memory import board as B
    from commons_memory import work as W
    a, b = store.ensure_agent("builder"), store.ensure_agent("asker5")
    r = W.start(store, a, "Ship the pricing page", project="site")
    assert {t["agent"]: t["state"] for t in ST.team(store)}["builder"] == "working"
    B.add(store, "question", "Annual or monthly first?", project="site", agent_id=b)
    team = ST.team(store)
    assert team[0]["agent"] == "asker5" and team[0]["state"] == "needs_you"          # the one waiting on you leads
    W.finish(store, a, r["claim_id"], "Pricing page live at /pricing\nwith tests")
    card = next(t for t in ST.team(store) if t["agent"] == "builder")
    assert card["state"] == "done" and card["headline"] == "Pricing page live at /pricing"


def test_handoff_commit_is_checked_on_github_through_the_vault(store, monkeypatch):
    from commons_memory import vault as V
    monkeypatch.setenv("COMMONS_VAULT_KEY", base64.b64encode(os.urandom(32)).decode())
    assert ST.git_verify(store, "o/r", "main", "zzz").startswith("unverified: not a commit")
    assert "no GitHub key" in ST.git_verify(store, "o/r", "main", "abc1234")
    V.add(store, "gh", "ghp_test_token_123456", "github", ["https://api.github.com"], {"bearer": True})
    calls = []
    def fake_call(s, cred, method, url, body="", content_type="", timeout=30):
        calls.append(url)
        if "/compare/" in url:
            return {"status": 200, "body": json.dumps({"status": "behind"})}
        return {"status": 200, "body": "{}"}
    monkeypatch.setattr(V, "call", fake_call)
    assert ST.git_verify(store, "Neoo-Blue/commons-memory", "main", "abc1234") == "verified on GitHub (on main)"
    assert calls[0].endswith("/repos/Neoo-Blue/commons-memory/commits/abc1234")


def test_an_open_work_claim_without_a_card_counts_as_working(store):
    a = store.ensure_agent("legacy")
    store.x("INSERT INTO work_claims(id,agent_id,what,status,started_at) VALUES('w1',?,'Add a show to Sonarr','active',1)", (a,))
    store.x("UPDATE agents SET last_seen=1 WHERE id=?", (a,))
    card = next(t for t in ST.team(store) if t["agent"] == "legacy")
    assert card["state"] == "working" and card["working_on"] == "Add a show to Sonarr"
