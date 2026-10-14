"""Dashboard transport and live-update hygiene: the page is precompressed, revalidates with a 304 over a kept-alive
connection, and views that only read never fire a live "change" (which re-renders every open dashboard)."""
import gzip
import http.client
import threading

from commons_memory import dashboard
from commons_memory.dashboard import change_fingerprint, make_server
from conftest import make_store


def test_page_is_precompressed_revalidated_and_kept_alive(tmp_path):
    srv = make_server(make_store(tmp_path, "perf.db"), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=8)
        c.request("GET", "/", headers={"Accept-Encoding": "gzip"})
        r = c.getresponse()
        body = r.read()
        assert r.status == 200 and r.getheader("Content-Encoding") == "gzip"
        assert gzip.decompress(body) == dashboard.PAGE_BYTES
        assert r.getheader("ETag") == dashboard.PAGE_ETAG and r.getheader("Cache-Control") == "private, no-cache"
        # the same socket again (HTTP/1.1 keep-alive): a browser that has this build gets a bodiless 304
        c.request("GET", "/", headers={"If-None-Match": dashboard.PAGE_ETAG})
        r = c.getresponse()
        assert r.status == 304 and r.read() == b""
        c.request("GET", "/api/overview")
        r = c.getresponse()
        assert r.status == 200 and r.getheader("Cache-Control") == "no-store" and b'"items"' in r.read()
        c.close()
    finally:
        srv.shutdown()


def test_read_only_views_do_not_fire_live_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # the Import preview scans ~/.claude/skills and friends
    store = make_store(tmp_path, "perf2.db")
    api = dashboard.API(store)
    fp = change_fingerprint(store)
    for _ in range(2):
        api.librarian({})
        api.find({"q": "billing"})
        api.palette({"q": "billing"})
        api.import_scan({"dir": str(tmp_path)})
    assert change_fingerprint(store) == fp
