from commons_memory import __version__
from commons_memory import releases as R


def test_release_channel_announces_once_and_tracks_what_each_agent_saw(store):
    es = R.entries()
    assert es and es[0]["version"] == __version__ and es[0]["changed"]
    a = store.ensure_agent("listener")
    r = R.announce(store)
    assert r and r["announced"] == __version__
    assert R.announce(store) is None                                             # once per version
    from commons_memory.messaging import Messaging
    import json
    inbox = json.dumps(Messaging(store).recv(a))
    assert f"v{__version__}" in inbox and "What changed" in inbox
    assert "COMMONS UPDATED" in R.nudge(store, a)
    wn = R.whats_new(store, a)
    assert wn["current"] == __version__ and wn["releases"]
    assert R.nudge(store, a) == ""                                               # seen
