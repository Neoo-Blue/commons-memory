import pytest

QUERIES = ["and or not", "& | ! <-> :*", "(plex) 'radarr' \"sonarr\"", "the a an of", "C++ & C#", "foo:bar AND NOT baz",
           "OR", "-", "a" * 300, "naïve café résumé", "日本語 テスト"]


@pytest.mark.parametrize("q", QUERIES)
def test_search_never_breaks_on_operator_like_or_odd_queries(store, q):
    store.add_item("memory", "Plex and Radarr run on the NAS; Sonarr handles TV, not movies")
    r = store.search(q, limit=5)                    # must not raise on either SQLite FTS5 or Postgres tsquery
    assert "text" in r


def test_operator_words_still_find_the_memory(store):
    store.add_item("memory", "Plex and Radarr run on the NAS; Sonarr handles TV, not movies")
    assert "Radarr" in store.search("radarr AND plex", mode="keyword")["text"]
    assert "Sonarr" in store.search("sonarr OR (tv)", mode="keyword")["text"]
