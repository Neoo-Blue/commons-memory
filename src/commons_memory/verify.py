"""Re-run an agent's `TESTS:` line in a fresh E2B sandbox, so "tests pass" is something the hub saw, not only what the
agent said. Runs when a step or work claim finishes with repo (owner/name) + commit and a line 'TESTS: <command> -> <result>',
and the vault holds an E2B key (service 'e2b'). Off otherwise. The run happens in the background: finishing never waits
on it. The outcome lands next to the result as verify.md, and a failure is sent to the agent.

The GitHub key (service 'github', the one git_verify uses) is used only to clone, then removed from the checkout before
the agent's command runs, so the command can't read it."""
from __future__ import annotations

import re
import threading

from .store import Store

TIMEOUT = 900  # seconds the command may run; the sandbox lives a little longer
_SLOTS = threading.BoundedSemaphore(2)  # sandboxes at once: each finish with a TESTS line would otherwise start one
TAIL = 4000

# ponytail: setup is guessed from lockfiles; add a per-repo setup hint if a repo needs more than this
SETUP = ("if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; "
         "elif [ -f package.json ]; then npm install --no-audit --no-fund; fi; "
         "if [ -f uv.lock ]; then pip install -q uv && uv sync -q --all-extras; "
         "elif [ -f pyproject.toml ]; then pip install -q -e '.[dev]' || pip install -q -e .; "
         "elif [ -f requirements.txt ]; then pip install -q -r requirements.txt; fi")


def tests_command(result: str) -> str:
    """The command from a 'TESTS: <command> -> <result>' line ('' if none)."""
    m = re.search(r"(?im)^\W*TESTS\s*:\s*(.+)$", result or "")
    if not m:
        return ""
    cmd = m.group(1).split(" -> ", 1)[0]
    return cmd.strip().strip("`").strip()


def _key(s: Store, service: str) -> str:
    from . import vault as V
    r = s.q("SELECT name FROM vault_creds WHERE service=? AND secret IS NOT NULL ORDER BY name LIMIT 1", (service,))
    return V._value(s, r[0]["name"]) if r else ""


def wanted(s: Store, result: str, changes: dict | None) -> str:
    """The command to verify, or '' when this finish isn't one to check."""
    c = changes or {}
    if not (re.fullmatch(r"[\w.-]+/[\w.-]+", c.get("repo") or "") and re.fullmatch(r"[0-9a-f]{7,40}", (c.get("commit") or "").strip())):
        return ""
    cmd = tests_command(result)
    if not cmd or not s.q("SELECT 1 FROM vault_creds WHERE service='e2b' AND secret IS NOT NULL LIMIT 1"):
        return ""
    return cmd


def run(s: Store, repo: str, commit: str, command: str, sandbox_cls=None) -> dict:
    """Clone repo at commit in a new sandbox, set it up, run command. {ok, exit_code, output, setup_failed?}."""
    if sandbox_cls is None:
        from e2b import Sandbox as sandbox_cls  # optional extra: pip install 'commons-memory[verify]'
    gh = _key(s, "github")
    url = f"https://{'x-access-token:' + gh + '@' if gh else ''}github.com/{repo}.git"
    sbx = sandbox_cls.create(timeout=TIMEOUT + 300, api_key=_key(s, "e2b"), metadata={"by": "commons-verify", "repo": repo})
    try:
        def sh(cmd: str, timeout: float) -> tuple[int, str]:
            try:
                r = sbx.commands.run(cmd, cwd="/home/user/repo", timeout=timeout)
                return r.exit_code, (r.stdout or "") + (r.stderr or "")
            except Exception as e:  # noqa: BLE001 - CommandExitException carries the code; anything else is a failed run
                return getattr(e, "exit_code", -1) or -1, (getattr(e, "stdout", "") or "") + (getattr(e, "stderr", "") or str(e))
        code, out = 0, ""
        try:
            sbx.commands.run(f"git clone -q {url} /home/user/repo && cd /home/user/repo && git checkout -q {commit} && "
                             f"git remote set-url origin https://github.com/{repo}.git", timeout=300)
        except Exception as e:  # noqa: BLE001
            msg = (getattr(e, "stderr", "") or str(e)).replace(gh, "***") if gh else (getattr(e, "stderr", "") or str(e))
            return {"ok": False, "exit_code": -1, "output": f"clone failed: {msg[-TAIL:]}", "setup_failed": True}
        code, out = sh(SETUP, 600)
        if code != 0:
            return {"ok": False, "exit_code": code, "output": out[-TAIL:], "setup_failed": True}
        code, out = sh(f'export PATH="$PWD/.venv/bin:$PWD/node_modules/.bin:$PATH"; {command}', TIMEOUT)
        # Prose after "TESTS:" ("npm test (backend), same as main") is not a failed test: the shell could not run it.
        not_a_command = code in (126, 127) or (code == 2 and "syntax error" in out.lower())
        return {"ok": code == 0, "exit_code": code, "output": out[-TAIL:]} | ({"setup_failed": True} if not_a_command else {})
    finally:
        try:
            sbx.kill()
        except Exception:  # noqa: BLE001 - it times out on its own
            pass


def report(command: str, repo: str, commit: str, r: dict) -> str:
    verdict = "passed" if r["ok"] else ("could not be set up" if r.get("setup_failed") else f"FAILED (exit {r['exit_code']})")
    return (f"# Verified run: {verdict}\n\nRepo {repo} at {commit[:12]}, in a fresh E2B sandbox.\n\n"
            f"Command: `{command}`\n\n```\n{r['output']}\n```\n")


def start(s: Store, agent_id: str, result: str, changes: dict | None, *, subtask: dict | None = None,
          claim: str | None = None, sandbox_cls=None, wait: bool = False) -> dict:
    """Check in the background if this finish carries a TESTS line to verify. Returns {verify: 'started'} or {}."""
    command = wanted(s, result, changes)
    if not command:
        return {}
    repo, commit = changes["repo"], changes["commit"].strip()

    def go():
        from . import workspace as W
        try:
            r = run(s, repo, commit, command, sandbox_cls)
        except Exception as e:  # noqa: BLE001 - a sandbox hiccup is recorded, not raised
            r = {"ok": False, "exit_code": -1, "output": f"sandbox error: {str(e)[:500]}", "setup_failed": True}
        where, project, ref = W._where(s, subtask, claim, None)
        W._drive(s).write(f"{where}/verify.md", report(command, repo, commit, r), project=project, agent_id=agent_id,
                          message="verified run" + (" passed" if r["ok"] else " failed"), link=ref)
        s.audit(agent_id, "verify." + ("pass" if r["ok"] else "fail"), ref, {"repo": repo, "commit": commit[:12], "exit": r["exit_code"]})
        if not r["ok"] and not r.get("setup_failed"):
            from .messaging import emit
            emit(s, agent_id, "verify_failed", ref=ref, preview=f"your TESTS line failed when re-run: {command[:80]} (see {where}/verify.md)")

    if not _SLOTS.acquire(blocking=False):
        return {"verify": "skipped: two verified runs are already going"}

    def held():
        try:
            go()
        finally:
            _SLOTS.release()

    if wait:
        held()
    else:
        threading.Thread(target=held, name="commons-verify", daemon=True).start()
    return {"verify": f"started: re-running '{command[:80]}' in a fresh sandbox; outcome goes to verify.md"}
