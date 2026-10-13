"""`commons wake daemon`: instant wake for CLI agents (v0.9.3). Standard library only, so it also runs from a thin client.

Holds the agent's SSE wake stream (/agent/stream) open with its agent key, and the moment an event arrives for the
agent (direct message, task assignment, lease expiry, debate turn, board @mention, escalation) it acts:

  --exec CMD     run CMD (shell) with the batch as JSON on stdin and in $COMMONS_WAKE_EVENTS / $COMMONS_WAKE_EVENT
  --tmux TARGET  type a wake prompt into a tmux pane (send-keys + Enter): wakes an interactive claude/codex/gemini
  --pty CMD      run CMD (e.g. `claude`) inside a PTY you use as your terminal and inject the wake prompt into it
  (none)         print one JSON line per batch to stdout

Bursts are coalesced: events arriving within --debounce seconds of each other fire one wake. On disconnect (server
restart, network blip) it reconnects with backoff and sends Last-Event-ID, so the server replays anything missed;
after a successful action it acks the newest event (POST /agent/wake/ack), so a restart of this daemon replays
from the ack too. Plain HTTP on a LAN/tailnet is fine: nothing needs a public address, the daemon dials out.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

PROMPT = ("[commons wake] {n} new event(s) for {agent}: {summary}. Call msg_recv / pickup() in Commons and handle them.")


def _key(args) -> str:
    if args.api_key:
        return args.api_key
    if os.environ.get("COMMONS_KEY"):
        return os.environ["COMMONS_KEY"]
    path = args.api_key_file or os.path.join(os.environ.get("COMMONS_HOME") or os.path.expanduser("~/.commons"),
                                             "agents", f"{args.agent}.key")
    with open(os.path.expanduser(path)) as f:
        return f.read().strip()


class Wake:
    def __init__(self, args, log=None):
        self.a = args
        self.base = args.url.rstrip("/").removesuffix("/mcp")
        self.key = _key(args)
        self.q: queue.Queue = queue.Queue()
        self.last_id = self._load_state()
        self.stop = threading.Event()
        self.connected = threading.Event()
        self.pty_fd = None
        self.logf = log or (lambda m: print(time.strftime("%H:%M:%S ") + m, file=sys.stderr, flush=True))

    # ---- state (Last-Event-ID survives daemon restarts; the server-side ack is the backstop)
    def _state_path(self):
        return os.path.expanduser(self.a.state or f"~/.commons/wake-{self.a.agent}.state")

    def _load_state(self):
        try:
            with open(self._state_path()) as f:
                return f.read().strip() or None
        except OSError:
            return None

    def _save_state(self, eid):
        try:
            os.makedirs(os.path.dirname(self._state_path()), exist_ok=True)
            with open(self._state_path(), "w") as f:
                f.write(eid)
        except OSError:
            pass

    # ---- stream reader (thread)
    def reader(self):
        backoff = 0.5
        while not self.stop.is_set():
            hdr = {"Authorization": f"Bearer {self.key}", "Accept": "text/event-stream"}
            if self.last_id:
                hdr["Last-Event-ID"] = self.last_id
            try:
                req = urllib.request.Request(self.base + "/agent/stream", headers=hdr)
                with urllib.request.urlopen(req, timeout=self.a.read_timeout) as r:  # noqa: S310 - user-configured URL
                    self.logf(f"stream open {self.base} as {self.a.agent}")
                    self.connected.set()
                    backoff = 0.5
                    ev, data = "message", []
                    for raw in r:
                        if self.stop.is_set():
                            return
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if not line:
                            if data and ev == "wake":
                                try:
                                    self.q.put(json.loads("\n".join(data)) | {"_rx": time.time()})
                                except ValueError:
                                    pass
                            ev, data = "message", []
                        elif line.startswith("event:"):
                            ev = line[6:].strip()
                        elif line.startswith("data:"):
                            data.append(line[5:].lstrip())
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    self.logf(f"auth rejected ({e.code}): check the agent key for {self.a.agent}")
                    if self.a.exit_on_auth_error:
                        self.q.put({"_fatal": f"auth {e.code}"})
                        return
                else:
                    self.logf(f"stream HTTP {e.code}")
            except Exception as e:  # noqa: BLE001 - reconnect on anything (server restart, timeout, reset)
                self.logf(f"stream dropped: {type(e).__name__}: {e}")
            self.connected.clear()
            if self.stop.wait(backoff):
                return
            backoff = min(backoff * 2, self.a.max_backoff)

    # ---- actions
    def summary(self, evs):
        """What gets TYPED into an agent's terminal (tmux/pty): event types and sender names only, reduced to a safe
        charset. Message text never goes in: it would read as the user's own words (prompt injection by any agent with
        a key), and a newline in it would submit extra prompts. The agent reads the messages via msg_recv, labelled."""
        safe = lambda v: re.sub(r"[^\w.@-]", "", str(v or ""))[:40]  # noqa: E731
        parts = [safe(e.get("event")) + (f" from {safe(e['sender'])}" if e.get("sender") else "") for e in evs[:5]]
        return "; ".join(parts) + (f" (+{len(evs) - 5} more)" if len(evs) > 5 else "")

    def act(self, evs) -> bool:
        clean = [{k: v for k, v in e.items() if not k.startswith("_")} for e in evs]
        prompt = PROMPT.format(n=len(evs), agent=self.a.agent, summary=self.summary(evs))
        if self.a.exec:
            env = os.environ | {"COMMONS_WAKE_EVENTS": json.dumps(clean), "COMMONS_WAKE_EVENT": json.dumps(clean[-1]),
                                "COMMONS_WAKE_PROMPT": prompt, "COMMONS_AGENT": self.a.agent}
            try:
                r = subprocess.run(self.a.exec, shell=True, input=json.dumps(clean), text=True, env=env,  # noqa: S602
                                   timeout=self.a.timeout, capture_output=True)
                if r.returncode:
                    self.logf(f"exec exited {r.returncode}: {(r.stderr or r.stdout)[-300:]}")
                return r.returncode == 0
            except (OSError, subprocess.TimeoutExpired) as e:
                self.logf(f"exec failed: {e}")
                return False
        if self.a.tmux:
            try:
                subprocess.run(["tmux", "send-keys", "-t", self.a.tmux, "-l", prompt], check=True, timeout=10)
                subprocess.run(["tmux", "send-keys", "-t", self.a.tmux, "Enter"], check=True, timeout=10)
                return True
            except (OSError, subprocess.SubprocessError) as e:
                self.logf(f"tmux inject failed: {e}")
                return False
        if self.pty_fd is not None:
            try:
                os.write(self.pty_fd, prompt.encode() + b"\r")
                return True
            except OSError as e:
                self.logf(f"pty inject failed: {e}")
                return False
        print(json.dumps({"wake": clean, "prompt": prompt}), flush=True)
        return True

    def ack(self, eid):
        self.last_id = eid
        self._save_state(eid)
        try:
            req = urllib.request.Request(self.base + "/agent/wake/ack", data=json.dumps({"event_id": eid}).encode(),
                                         method="POST", headers={"Authorization": f"Bearer {self.key}",
                                                                 "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()  # noqa: S310
        except Exception as e:  # noqa: BLE001 - ack is best effort; Last-Event-ID still covers us
            self.logf(f"ack failed: {e}")

    # ---- main loop: debounce bursts into one wake
    def run(self, max_wakes: int = 0) -> int:
        threading.Thread(target=self.reader, daemon=True, name="commons-wake-stream").start()
        wakes = 0
        while not self.stop.is_set():
            try:
                first = self.q.get(timeout=1)
            except queue.Empty:
                continue
            if "_fatal" in first:
                return 3
            batch, t_end = [first], time.time() + self.a.max_batch_wait
            while True:
                try:
                    batch.append(self.q.get(timeout=max(0.0, min(self.a.debounce, t_end - time.time()))))
                except queue.Empty:
                    break
                if time.time() >= t_end:
                    break
            seen, uniq = set(), []
            for e in batch:
                if e.get("event_id") not in seen:
                    seen.add(e.get("event_id"))
                    uniq.append(e)
            self.logf(f"wake: {len(uniq)} event(s) -> {self.summary(uniq)[:160]}")
            ok = False
            for attempt in range(3):
                ok = self.act(uniq)
                if ok:
                    break
                time.sleep(2 * (attempt + 1))
            if ok and uniq[-1].get("event_id"):
                self.ack(uniq[-1]["event_id"])
            wakes += 1
            if max_wakes and wakes >= max_wakes:
                self.stop.set()
        return 0

    # ---- PTY mode
    def run_pty(self, cmd: str) -> int:
        import pty
        import select
        import termios
        import tty
        pid, fd = pty.fork()
        if pid == 0:
            os.execvp("/bin/sh", ["/bin/sh", "-c", cmd])
        self.pty_fd = fd
        threading.Thread(target=self.run, daemon=True).start()
        old = None
        if os.isatty(0):
            old = termios.tcgetattr(0)
            tty.setraw(0)
        try:
            while True:
                rl, _, _ = select.select([fd, 0], [], [])
                if fd in rl:
                    try:
                        b = os.read(fd, 65536)
                    except OSError:
                        break
                    if not b:
                        break
                    os.write(1, b)
                if 0 in rl:
                    b = os.read(0, 65536)
                    if not b:
                        break
                    os.write(fd, b)
        finally:
            if old:
                termios.tcsetattr(0, termios.TCSADRAIN, old)
            self.stop.set()
        return os.waitpid(pid, 0)[1] >> 8


def parser(p=None):
    p = p or argparse.ArgumentParser(prog="commons wake daemon", description=__doc__.split("\n\n")[0])
    p.add_argument("--agent", required=True)
    p.add_argument("--url", default=os.environ.get("COMMONS_URL", "http://127.0.0.1:8770"))
    p.add_argument("--api-key", default="")
    p.add_argument("--api-key-file", default="")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--exec", default="", help="shell command to run per wake (events JSON on stdin)")
    g.add_argument("--tmux", default="", help="tmux target pane to type the wake prompt into")
    g.add_argument("--pty", default="", help="run this agent CLI in a PTY and inject wake prompts into it")
    p.add_argument("--debounce", type=float, default=1.0, help="coalesce events this close together into one wake (s)")
    p.add_argument("--max-batch-wait", type=float, default=5.0, help="never delay a wake longer than this (s)")
    p.add_argument("--timeout", type=int, default=600, help="--exec timeout (s)")
    p.add_argument("--read-timeout", type=float, default=45.0, help="reconnect if the stream is silent this long")
    p.add_argument("--max-backoff", type=float, default=10.0)
    p.add_argument("--state", default="", help="file holding the last handled event id")
    p.add_argument("--exit-on-auth-error", action="store_true")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    w = Wake(args)
    try:
        return w.run_pty(args.pty) if args.pty else w.run()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
