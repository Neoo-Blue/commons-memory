

def test_looking_at_the_archive_from_the_dashboard_leaves_it_archived(store):
    from commons_memory.tiers import set_tier
    iid = store.add_item("memory", "the archived note about the old relay")["id"]
    set_tier(store, iid, "cold")
    store.search("archived note relay", touch=False)
    store.expand(iid, touch=False)
    assert store.q("SELECT tier, last_access FROM items WHERE id=?", (iid,))[0]["last_access"] is None
    store.search("archived note relay")
    assert store.q("SELECT last_access FROM items WHERE id=?", (iid,))[0]["last_access"] is not None
