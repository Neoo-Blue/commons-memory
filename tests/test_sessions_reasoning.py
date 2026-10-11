def test_session_checkpoint_handoff_resume(store):
    a = store.ensure_agent("claude-code")
    b = store.ensure_agent("cursor")
    s = store.session_start("Migrate auth to OAuth", a, "app")
    sid = s["session_id"]
    store.checkpoint(sid, "replaced login form", {"done": ["login form"]}, a)
    d = store.record_decision("Use PKCE flow", "public client, no secret", ["implicit flow"], a, "app", sid)
    h = store.handoff(sid, summary="half done", state="login works, refresh tokens pending",
                      decisions=["Use PKCE"], next_steps=["implement refresh"], open_questions=["token TTL?"],
                      relevant_ids=[d["id"]], to_agent="cursor", agent_id=a)
    assert h["packet"]["next_steps"] == ["implement refresh"] and h["task_id"]
    assert store.inbox(b)[0]["kind"] == "handoff"
    r = store.resume(project="app", agent_id=b)
    assert r["session_id"] == sid and r["handoff"]["open_questions"] == ["token TTL?"]
    assert r["state"] == {"done": ["login form"]}
    assert any(i["kind"] == "decision" for i in r["session_items"])
    assert store.q("SELECT status, agent_id FROM sessions WHERE id=?", (sid,))[0]["agent_id"] == b


def test_why_by_text_and_by_id(store):
    a = store.ensure_agent("gpt")
    sym = store.upsert_entity("function", "rate_limit", store.project_id("api"), "api.py", 3)
    d = store.record_decision("Token bucket for rate limiting", "smooth bursts", ["fixed window"], a, "api",
                              related_ids=[sym])
    r = store.why("why did we pick token bucket rate limiting")
    assert r["decisions"][0]["id"] == d["id"] and r["decisions"][0]["alternatives"] == ["fixed window"]
    r2 = store.why(sym)
    assert r2["decisions"][0]["why"] == "smooth bursts" and r2["decisions"][0]["agent"] == "gpt"
