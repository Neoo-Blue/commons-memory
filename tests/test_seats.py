"""API seats: models from your own keys take steps only when no subscription agent is free, by tier, under a cap."""
from commons_memory import seats, settings
from commons_memory.messaging import heartbeat
from commons_memory.orchestrator import Orchestrator
from commons_memory.providers import ProviderLayer


def _setup(store, on=True):
    ProviderLayer(store).add_provider("mock", "mock")
    settings.put(store, "seat_models", ["mock/m-cheap", "mock/m-mid", "mock/m-top"])
    settings.put(store, "api_seats", on)
    o = Orchestrator(store)
    rev = store.ensure_agent("reviewer")
    o.profiles.set(rev, expertise={"review": 0.9, "code": 0.8}, cost=3, speed=3, max_concurrent=3)
    return o, rev


def test_seats_are_off_until_you_switch_them_on(store):
    o, _ = _setup(store, on=False)
    assert seats.sync(store) is None
    jid = o.submit("review the README", dag=[{"key": "r", "type": "review", "title": "review"}])["job_id"]
    assert store.q("SELECT assigned_agent FROM subtasks WHERE job_id=?", (jid,))[0]["assigned_agent"] != \
        (store.get_agent("api-seats") or {}).get("id")


def test_a_free_subscription_agent_always_wins_and_the_seat_takes_the_rest(store):
    o, rev = _setup(store)
    seat = seats.sync(store)
    heartbeat(store, rev, status="online", client="test")
    a = o.submit("review A", dag=[{"key": "r", "type": "review", "title": "review A"}])["job_id"]
    assert store.q("SELECT assigned_agent FROM subtasks WHERE job_id=?", (a,))[0]["assigned_agent"] == rev
    store.x("UPDATE agent_presence SET last_heartbeat=0 WHERE agent_id=?", (rev,))  # the reviewer goes away
    b = o.submit("review B", dag=[{"key": "r", "type": "review", "title": "review B"}])["job_id"]
    assert store.q("SELECT assigned_agent FROM subtasks WHERE job_id=?", (b,))[0]["assigned_agent"] == seat
    c = o.submit("fix the bug", dag=[{"key": "c", "type": "code", "title": "fix it"}])["job_id"]
    assert store.q("SELECT assigned_agent FROM subtasks WHERE job_id=?", (c,))[0]["assigned_agent"] != seat  # needs a repo


def test_the_seat_runs_its_steps_on_the_cards_tier_and_records_spend_once(store):
    o, rev = _setup(store)
    seat = seats.sync(store)
    store.x("UPDATE agents SET last_seen=0 WHERE id=?", (rev,))
    jid = o.submit("summarize the incident notes", dag=[{"key": "s", "type": "summarize", "title": "summarize",
                                                        "card": {"criticality": "low"}}])["job_id"]
    sid = store.q("SELECT id FROM subtasks WHERE job_id=?", (jid,))[0]["id"]
    assert store.q("SELECT model FROM routing_decisions WHERE subtask_id=?", (sid,))[0]["model"] == "mock/m-cheap"
    assert seats.run_once(store) == 1
    assert store.q("SELECT status FROM jobs WHERE id=?", (jid,))[0]["status"] == "done"
    rows = store.q("SELECT cost FROM usage WHERE agent_id=? AND job_id=?", (seat, jid))
    assert len(rows) == 1  # one usage row per run: the cap and the job budget see the same spend


def test_the_daily_cap_pauses_the_seats_and_tells_you(store):
    o, rev = _setup(store)
    seat = seats.sync(store)
    store.x("UPDATE agents SET last_seen=0 WHERE id=?", (rev,))
    settings.put(store, "seat_daily_cap", 0)
    o.submit("summarize", dag=[{"key": "s", "type": "summarize", "title": "summarize"}])
    assert seats.run_once(store) == 0
    assert store.q("SELECT 1 FROM notifications WHERE title LIKE 'API seats reached%'")
    assert seats.status(store)["capped"] and seat
