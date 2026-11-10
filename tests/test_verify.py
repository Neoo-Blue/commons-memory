import base64
import os

from commons_memory import vault as V
from commons_memory import verify


class FakeSandbox:
    """Records what would run in E2B; `fail` makes the agent's command exit 1."""
    ran, created, killed, fail = [], [], [], False

    class _R:
        def __init__(self, code, out):
            self.exit_code, self.stdout, self.stderr = code, out, ""

    @classmethod
    def create(cls, **kw):
        cls.created.append(kw)
        return cls()

    @property
    def commands(self):
        return self

    def run(self, cmd, cwd=None, timeout=None):
        FakeSandbox.ran.append(cmd)
        if FakeSandbox.fail and "pytest" in cmd:
            e = Exception("exit 1")
            e.exit_code, e.stdout, e.stderr = 1, "1 failed", ""
            raise e
        return self._R(0, "3 passed")

    def kill(self):
        FakeSandbox.killed.append(1)


def _reset():
    FakeSandbox.ran, FakeSandbox.created, FakeSandbox.killed, FakeSandbox.fail = [], [], [], False


def test_tests_command_parsing():
    assert verify.tests_command("done\nTESTS: pytest -q tests/x.py -> 3 passed") == "pytest -q tests/x.py"
    assert verify.tests_command("TESTS: `npm test`") == "npm test"
    assert verify.tests_command("no evidence here") == ""


def test_off_without_e2b_key_or_commit(store, monkeypatch):
    monkeypatch.setenv("COMMONS_VAULT_KEY", base64.b64encode(os.urandom(32)).decode())
    ch = {"repo": "Neoo-Blue/x", "commit": "abc1234"}
    assert verify.wanted(store, "TESTS: pytest -> ok", ch) == ""  # no e2b key in the vault
    V.add(store, "e2b-api-key", "e2b_test", "e2b", ["https://api.e2b.app"], {"header": "X-API-Key"})
    assert verify.wanted(store, "TESTS: pytest -> ok", ch) == "pytest"
    assert verify.wanted(store, "TESTS: pytest -> ok", {"repo": "x", "commit": "abc1234"}) == ""
    assert verify.wanted(store, "TESTS: pytest -> ok", {"repo": "Neoo-Blue/x", "commit": "main"}) == ""


def test_run_clones_without_leaving_the_github_key(store, monkeypatch):
    monkeypatch.setenv("COMMONS_VAULT_KEY", base64.b64encode(os.urandom(32)).decode())
    V.add(store, "e2b-api-key", "e2b_test", "e2b", ["https://api.e2b.app"], {"header": "X-API-Key"})
    V.add(store, "gh", "ghp_secret", "github", ["https://api.github.com"], {"bearer": True})
    _reset()
    r = verify.run(store, "Neoo-Blue/x", "abc1234", "pytest -q", FakeSandbox)
    assert r["ok"] and FakeSandbox.killed and FakeSandbox.created[0]["api_key"] == "e2b_test"
    clone, setup, cmd = FakeSandbox.ran
    assert "ghp_secret" in clone and "set-url origin https://github.com/Neoo-Blue/x.git" in clone
    assert "ghp_secret" not in setup + cmd and cmd.endswith("pytest -q")
    FakeSandbox.fail = True
    r = verify.run(store, "Neoo-Blue/x", "abc1234", "pytest -q", FakeSandbox)
    assert not r["ok"] and r["exit_code"] == 1 and "1 failed" in r["output"]
    assert "FAILED (exit 1)" in verify.report("pytest -q", "Neoo-Blue/x", "abc1234", r)
