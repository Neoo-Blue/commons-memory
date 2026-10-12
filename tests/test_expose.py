"""Networking: providers driven through mock CLIs; auth guard; Cloudflare Access JWT; snippets; service units."""
import http.server
import json
import os
import stat
import threading
import time

import pytest

from commons_memory import expose as X
from commons_memory.store import CommonsError


def _cli(bin_, name, script):
    f = bin_ / name
    f.write_text("#!/bin/sh\n" + script)
    f.chmod(f.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def env(tmp_path, monkeypatch):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "calls.log"
    _cli(bin_, "tailscale", f'echo "tailscale $@" >> {log}\nif [ "$1" = status ]; then echo \'{{"BackendState":"Running",'
         '"Self":{"DNSName":"box.tail123.ts.net.","TailscaleIPs":["100.64.0.7","fd7a::7"],"Online":true}}\'; fi\n')
    _cli(bin_, "cloudflared", f'echo "cloudflared $@" >> {log}\necho "INF Requesting new quick Tunnel"\n'
         'echo "INF |  https://brave-otter-12.trycloudflare.com  |"\necho "INF Registered tunnel connection"\nexec sleep 30\n')
    _cli(bin_, "ngrok", f'echo "ngrok $@" >> {log}\necho \'{{"lvl":"info","msg":"started tunnel","url":"https://ab12.ngrok.app"}}\'\nexec sleep 30\n')
    _cli(bin_, "ssh", f'echo "ssh $@" >> {log}\necho "abc.lhr.life tunneled with tls termination, https://abc.lhr.life"\nexec sleep 30\n')
    _cli(bin_, "bore", f'echo "bore $@" >> {log}\necho "listening at bore.pub:41234"\nexec sleep 30\n')
    _cli(bin_, "zerotier-cli", 'echo \'[{"assignedAddresses":["fc00::1/40","10.147.17.5/24"]}]\'\n')
    _cli(bin_, "ip", 'echo "4: wg0: <POINTOPOINT> mtu 1420\n    inet 10.8.0.2/24 scope global wg0"\n')
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("COMMONS_HOME", str(tmp_path / "ch"))
    return log


def _server(code):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_POST(self):  # noqa: N802
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b"{}")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def test_public_exposure_refused_without_auth(store, env):
    srv, port = _server(200)  # answers unauthenticated
    with pytest.raises(CommonsError, match="refusing public exposure"):
        X.up(store, "cloudflare", port=port)
    with pytest.raises(CommonsError, match="not reachable"):
        X.up(store, "ngrok", port=1)
    srv.shutdown()
    srv, port = _server(401)
    os.environ["COMMONS_REQUIRE_KEYS"] = "0"
    try:
        with pytest.raises(CommonsError, match="COMMONS_REQUIRE_KEYS=0"):
            X.up(store, "tailscale", port=port, public=True)
    finally:
        os.environ.pop("COMMONS_REQUIRE_KEYS")
    srv.shutdown()


def test_tailscale_serve_funnel_bind_and_down(store, env):
    srv, port = _server(401)
    st = X.up(store, "tailscale", port=port)
    assert st["url"] == "https://box.tail123.ts.net" and not st["public"] and st["mode"] == "serve"
    calls = env.read_text()
    assert f"tailscale serve --bg --https=443 http://127.0.0.1:{port}" in calls
    st = X.up(store, "tailscale", port=port, public=True)
    assert st["public"] and st["warnings"] and "tailscale funnel --bg" in env.read_text()
    assert X.bind_host("tailscale") == "100.64.0.7"
    b = X.up(store, "tailscale", port=port, https_port=0)
    assert b["url"] == f"http://100.64.0.7:{port}" and b["bind"] == "100.64.0.7"
    X.up(store, "tailscale", port=port)
    X.down(store)
    assert "tailscale serve --https=443 off" in env.read_text() and X.state(store)["provider"] == "local"
    srv.shutdown()


def test_tunnels_cloudflare_ngrok_ssh_bore(store, env, tmp_path):
    srv, port = _server(401)
    st = X.up(store, "cloudflare", port=port)
    assert st["url"] == "https://brave-otter-12.trycloudflare.com" and st["public"] and st["pid"]
    pid = st["pid"]
    X.down(store)
    time.sleep(0.2)
    with pytest.raises(OSError):
        os.kill(pid, 0) if not os.waitpid(pid, os.WNOHANG)[0] else (_ for _ in ()).throw(OSError())
    tf = tmp_path / "tok"
    tf.write_text("t")
    st = X.up(store, "cloudflare", port=port, name="commons", hostname="mem.example.com", token_file=str(tf))
    assert st["url"] == "https://mem.example.com" and "--token-file" in env.read_text()
    X.down(store)
    st = X.up(store, "ngrok", port=port, oauth="google", oauth_allow=["me@x.com"], cidr_allow=["1.2.3.0/24"])
    assert st["url"] == "https://ab12.ngrok.app"
    c = env.read_text()
    assert "--oauth=google" in c and "--oauth-allow-email=me@x.com" in c and "--cidr-allow=1.2.3.0/24" in c
    X.down(store)
    assert X.up(store, "ssh", port=port)["url"] == "https://abc.lhr.life" and "nokey@localhost.run" in env.read_text()
    X.down(store)
    with pytest.raises(CommonsError, match="plain TCP"):
        X.up(store, "bore", port=port)
    assert X.up(store, "bore", port=port, allow_plain_http=True)["url"] == "http://bore.pub:41234"
    st = X.state(store)
    X._save(store, st | {"url": f"http://127.0.0.1:{port}"})  # don't touch the internet from tests
    h = X.health(store)
    assert h["health"]["ok"] and h["health"]["process_alive"]
    X.down(store)
    srv.shutdown()


def test_overlay_bind_snippets_proxy_service(store, env, tmp_path):
    assert X.bind_host("zerotier") == "10.147.17.5" and X.bind_host("wireguard") == "10.8.0.2"
    srv, port = _server(401)
    X.up(store, "tailscale", port=port)
    d = tmp_path / "ch" / "connect"
    assert "https://box.tail123.ts.net/mcp" in (d / "cursor-mcp.json").read_text()
    assert "claude mcp add --transport http commons https://box.tail123.ts.net/mcp" in (d / "claude-code.sh").read_text()
    X.down(store)
    assert "127.0.0.1:8765/mcp" in (d / "cursor-mcp.json").read_text()  # snippets follow the reachable URL
    assert "reverse_proxy 127.0.0.1:8765" in X.proxy_config("caddy", "m.example.com", 8765)
    assert "proxy_buffering off" in X.proxy_config("nginx", "m.example.com", 8765)
    assert "ExecStart=" in X.service_unit("systemd") and "<plist" in X.service_unit("launchd") and "nssm" in X.service_unit("windows")
    srv.shutdown()


def test_cloudflare_access_jwt_in_middleware(store, monkeypatch):
    import asyncio

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    from commons_memory.auth import PRINCIPAL, AuthMiddleware
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "k1", "alg": "RS256", "use": "sig"}

    class Certs(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):  # noqa: N802
            b = json.dumps({"keys": [jwk]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Certs)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("COMMONS_CF_ACCESS_TEAM", "acme")
    monkeypatch.setenv("COMMONS_CF_ACCESS_AUD", "aud123")
    monkeypatch.setenv("COMMONS_CF_ACCESS_CERTS", f"http://127.0.0.1:{srv.server_address[1]}/certs")
    monkeypatch.setenv("COMMONS_CF_ACCESS_AS_AUTH", "1")

    def tok(aud="aud123", **claims):
        return jwt.encode({"aud": aud, "exp": time.time() + 60, **claims}, key, algorithm="RS256", headers={"kid": "k1"})
    seen = {}

    async def app(scope, receive, send):
        seen["p"] = PRINCIPAL.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    mw = AuthMiddleware(app, store, allow_anon_reads=False)

    def call(h):
        out = []

        async def rcv():
            return {"type": "http.request", "body": b""}

        async def snd(m):
            out.append(m)
        asyncio.run(mw({"type": "http", "path": "/mcp", "client": ("127.0.0.1", 1),
                        "headers": [(k.encode(), v.encode()) for k, v in h.items()]}, rcv, snd))
        return out[0]["status"]
    key_ = store.register_agent("direct")["api_key"]
    assert call({"authorization": f"Bearer {key_}"}) == 200  # direct (no cf-ray): Access not involved
    assert call({"cf-ray": "x", "authorization": f"Bearer {key_}"}) == 403  # via Cloudflare without JWT
    assert call({"cf-ray": "x", "cf-access-jwt-assertion": tok(aud="other")}) == 403
    assert call({"cf-ray": "x", "cf-access-jwt-assertion": tok(common_name="svc-token-1")}) == 200
    assert store.get_agent(seen["p"])["name"] == "cf:svc-token-1"
    assert call({"cf-ray": "x", "cf-access-jwt-assertion": tok(email="me@acme.com"), "authorization": f"Bearer {key_}"}) == 200
    assert store.get_agent(seen["p"])["name"] == "direct"
    srv.shutdown()


def test_status_cli_shows_network(store, env, capsys):
    from commons_memory.cli import main
    srv, port = _server(401)
    X.up(store, "tailscale", port=port)
    main(["--db", store.path, "status", "--no-probe"])
    out = capsys.readouterr().out
    assert "tailscale -> https://box.tail123.ts.net" in out
    srv.shutdown()
