"""Who is on the team: cards the hub writes, newcomers met at pickup (never a broadcast), names only the user sets."""
import json
import time

import pytest

from commons_memory import team as T
from commons_memory.connect import create_key
from commons_memory.messaging import Messaging
from commons_memory.orchestrator import Orchestrator
from commons_memory.store import CommonsError


def _team(store):
    claude = create_key(store, "Claude", platform="claude-code")["id"]
    bot = create_key(store, "grok", platform='Grok Bot "Botty" on the tailnet')["id"]
    Orchestrator(store).profiles.set(bot, expertise={"research": 0.9, "x-search": 0.8}, strengths=["live X search"])
    for a in (claude, bot):
        store.x("UPDATE agents SET last_seen=? WHERE id=?", (time.time(), a))
    store.ensure_agent("user")                                   # the hub's own identities never show
    store.ensure_agent("provider:anthropic")
    store.register_agent("v07-smoke")                            # a test key that never did anything
    store.x("UPDATE agents SET last_seen=? WHERE name='v07-smoke'", (time.time() - 10 * 86400,))
    return claude, bot


def test_the_card_keeps_what_the_hub_checked_apart_from_what_the_agent_claims(store):
    claude, bot = _team(store)
    assert {m["name"] for m in T.members(store)} == {"Claude", "grok"}
    c = T.card(store, "grok")
    assert c["key"]["issued_by"] == "the dashboard's Connect page" and c["joined"]
    assert c["claimed"]["expertise"]["research"] == 0.9 and "live X search" in c["claimed"]["strengths"]
    assert c["measured"]["by_type"] == {} and "headroom" in c and c["presence"]["state"]
    with pytest.raises(CommonsError, match="no teammate"):
        T.card(store, "v07-smoke")


def test_pickup_shows_the_team_once_then_only_newcomers(store):
    claude, bot = _team(store)
    o = Orchestrator(store)
    first = o.pickup(claude)["text"]
    assert "YOUR TEAM" in first and "grok" in first and "good at research" in first and "(claimed)" in first
    assert "Claude" not in first.split("YOUR TEAM")[1].split("\n\n")[0].split("\n", 1)[1]   # not itself
    again = o.pickup(claude)["text"]
    assert "YOUR TEAM" not in again and "NEW ON THE TEAM" not in again
    assert "TEAM (who(name) for a card): grok" in again           # every later session still learns who is there
    time.sleep(0.01)
    muse = create_key(store, "muse", platform="Muse (Meta)")["id"]
    store.x("UPDATE agents SET last_seen=? WHERE id=?", (time.time(), muse))
    news = o.pickup(claude)["text"]
    assert "NEW ON THE TEAM" in news and "muse" in news and "grok" not in news.split("NEW ON THE TEAM")[1]
    mine = o.pickup(muse)["text"]                                # the newcomer meets everyone at its first check-in
    assert "YOUR TEAM" in mine and "Claude" in mine and "grok" in mine
    assert not store.q("SELECT 1 FROM message_deliveries")       # nobody was messaged, so nobody was woken


def test_only_the_user_names_an_agent_and_no_name_can_pass_for_another(store):
    claude, bot = _team(store)
    assert T.set_display(store, "grok", "Grok bot (Botty)")["display"] == "Grok bot (Botty)"
    for taken in ("Claude", "claude", "commons"):                 # another agent's handle, or the hub's
        with pytest.raises(CommonsError, match="another agent's name"):
            T.set_display(store, "grok", taken)
    with pytest.raises(CommonsError, match="another agent's name"):
        T.set_display(store, "Claude", "Grok bot (botty)")         # another agent's display name
    assert T.card(store, "grok")["display"] == "Grok bot (Botty)"
    assert T.card(store, "grok bot")["name"] == "grok"             # the name the user says finds the handle to send to
    Messaging(store).send(claude, "grok", "still reaches it by its handle")
    assert Messaging(store).recv(bot)[0]["text"] == "still reaches it by its handle"
    assert "Grok bot (Botty) (`grok`)" in T.line(store, store.get_agent("grok"))
    assert T.set_display(store, "grok", "")["display"] == "grok"


def test_an_introduction_is_one_note_each_from_the_hub_and_wakes_nobody(store):
    claude, bot = _team(store)
    muse = create_key(store, "muse")["id"]
    out = T.introduce(store, "muse", "grok", "you run the Grok marketing together")
    assert out["introduced"] == ["muse", "grok"] and len(out["messages"]) == 2
    m = Messaging(store).recv(muse)[0]
    assert m["from"] == "commons" and m["origin"]["by"] == "the user's introduction" and "grok" in m["text"]
    assert "marketing" in Messaging(store).recv(bot)[0]["text"]
    assert Messaging(store).recv(claude) == []                   # nobody else hears about it
    assert not store.q("SELECT 1 FROM wake_events")              # read at the next check-in, not woken for
    with pytest.raises(CommonsError, match="say why"):
        T.introduce(store, "muse", "grok", "hi")


@pytest.mark.asyncio
async def test_who_is_a_core_tool_and_the_dashboard_shows_identity(tmp_path, store):
    from commons_memory.dashboard import API
    from commons_memory.server import build_server
    claude, bot = _team(store)
    key = store.register_agent("Claude")["api_key"]
    mcp = build_server(store)
    res = await mcp.call_tool("who", {"api_key": key})
    team = json.loads(list(res)[0].text if hasattr(list(res)[0], "text") else list(res)[0][0].text)["team"]
    assert {t["name"] for t in team} == {"Claude", "grok"}
    res = await mcp.call_tool("who", {"name": "grok", "api_key": key})
    d = API(store).agent_detail({"name": "grok"})
    assert d["identity"]["key"]["issued_by"] and {t["name"] for t in d["team"]} == {"Claude"}
    from commons_memory.toolprofile import CORE
    assert "who" in CORE and "msg_get" in CORE


def test_review_fixes_late_connecting_keys_unicode_names_and_hub_identities(store):
    from commons_memory.dashboard import OWNER_ONLY
    claude, bot = _team(store)
    o = Orchestrator(store)
    o.pickup(claude)                                              # Claude has met the team
    late = create_key(store, "muse")["id"]                        # minted now, connects later (last_seen = 0 until then)
    assert "muse" not in o.pickup(claude)["text"]
    store.x("UPDATE agents SET last_seen=? WHERE id=?", (time.time(), late))
    assert "NEW ON THE TEAM" in o.pickup(claude)["text"]          # met when it first shows up, not missed for good
    assert T.nudge(store, None) == "" and not store.q("SELECT 1 FROM meta WHERE key='team_met:None'")
    T.set_display(store, "grok", "Über Botty")
    with pytest.raises(CommonsError, match="another agent's name"):
        T.set_display(store, "muse", "über botty")                # any script, any case
    for hub in ("librarian", "Dashboard-User", "provider:x"):
        with pytest.raises(CommonsError, match="another agent's name"):
            T.set_display(store, "muse", hub)
    assert T.set_display(store, "muse", "a" * 59 + " b")["display"] == "a" * 59
    with pytest.raises(CommonsError, match="display name"):
        store.register_agent("ÜBER BOTTY")                        # nor register under someone's display name
    assert {"/api/agent/display", "/api/agent/introduce", "/api/msg/send"} <= set(OWNER_ONLY)
