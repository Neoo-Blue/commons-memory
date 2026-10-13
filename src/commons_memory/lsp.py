"""Live code lookups through each language's own server (LSP): definition, references and hover with real types.

The code index answers "where is X and what calls it" from a static parse; a language server answers with the
compiler's view (overloads, generics, imports through re-exports, types on hover). Commons resolves a symbol name to
a file and line through its own index, then asks the server installed on this machine: basedpyright/pyright/pylsp,
typescript-language-server, gopls, rust-analyzer, clangd, sourcekit-lsp, jdtls, csharp-ls, ruby-lsp (first one found;
COMMONS_LSP_<LANG>="cmd args" overrides). Servers run on the machine that has the repo, so the MCP tool is limited to
the local owner and COMMONS_ADMIN_AGENTS, like the other host-filesystem tools.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse

from .store import CommonsError, Store

EXT = {".py": "python", ".ts": "typescript", ".tsx": "typescriptreact", ".js": "javascript", ".jsx": "javascriptreact",
       ".mjs": "javascript", ".go": "go", ".rs": "rust", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp",
       ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".swift": "swift", ".java": "java", ".cs": "csharp", ".rb": "ruby"}
SERVERS = {
    "python": [["basedpyright-langserver", "--stdio"], ["pyright-langserver", "--stdio"], ["pylsp"],
               ["jedi-language-server"]],
    "typescript": [["typescript-language-server", "--stdio"]], "go": [["gopls"]], "rust": [["rust-analyzer"]],
    "c": [["clangd"]], "cpp": [["clangd"]], "swift": [["sourcekit-lsp"]], "java": [["jdtls"]], "csharp": [["csharp-ls"]],
    "ruby": [["ruby-lsp"]]}
FAMILY = {"typescriptreact": "typescript", "javascript": "typescript", "javascriptreact": "typescript"}
METHODS = {"definition": "textDocument/definition", "references": "textDocument/references", "hover": "textDocument/hover"}


def server_for(lang: str) -> list[str] | None:
    fam = FAMILY.get(lang, lang)
    env = os.environ.get(f"COMMONS_LSP_{fam.upper()}")
    if env:
        return shlex.split(env)
    return next((c for c in SERVERS.get(fam, []) if shutil.which(c[0])), None)


def _uri(p: Path) -> str:
    return p.resolve().as_uri()


class Client:
    """A minimal JSON-RPC client over stdio (Content-Length framing), enough for lookups."""

    def __init__(self, cmd: list[str], root: Path, timeout: float = 20):
        self.root, self.timeout, self.n = root, timeout, 0
        self.p = subprocess.Popen(cmd, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.replies: dict[int, dict] = {}
        self.cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()
        self.request("initialize", {"processId": os.getpid(), "rootUri": _uri(root), "capabilities": {
            "textDocument": {"hover": {"contentFormat": ["plaintext", "markdown"]}}},
            "workspaceFolders": [{"uri": _uri(root), "name": root.name}]})
        self.notify("initialized", {})

    def _send(self, msg: dict) -> None:
        body = json.dumps(msg).encode()
        self.p.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        self.p.stdin.flush()

    def _read(self) -> None:
        out = self.p.stdout
        while True:
            size = 0
            while True:
                line = out.readline()
                if not line:
                    return
                if line in (b"\r\n", b"\n"):
                    break
                if line.lower().startswith(b"content-length:"):
                    size = int(line.split(b":")[1])
            msg = json.loads(out.read(size))
            if "id" in msg and "method" in msg:  # the server asking us (configuration, progress): answer politely
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": [] if msg["method"] == "workspace/configuration" else None})
            elif "id" in msg:
                with self.cv:
                    self.replies[msg["id"]] = msg
                    self.cv.notify_all()

    def request(self, method: str, params: dict):
        self.n += 1
        rid = self.n
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        with self.cv:
            if not self.cv.wait_for(lambda: rid in self.replies, self.timeout):
                raise CommonsError(f"language server didn't answer {method} within {self.timeout:.0f}s")
            msg = self.replies.pop(rid)
        if "error" in msg:
            raise CommonsError(f"language server: {msg['error'].get('message')}")
        return msg.get("result")

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def open(self, path: Path, lang: str) -> None:
        self.notify("textDocument/didOpen", {"textDocument": {"uri": _uri(path), "languageId": lang, "version": 1,
                                                                "text": path.read_text(errors="replace")}})

    def close(self) -> None:
        try:
            self.request("shutdown", {})
            self.notify("exit", {})
        except Exception:  # noqa: BLE001 - it is going away either way
            pass
        try:
            self.p.wait(3)
        except subprocess.TimeoutExpired:
            self.p.kill()


def _loc(root: Path, loc: dict) -> dict:
    uri = loc.get("uri") or loc.get("targetUri")
    rng = loc.get("range") or loc.get("targetSelectionRange") or {}
    p = Path(unquote(urlparse(uri).path))
    line = rng.get("start", {}).get("line", 0)
    try:
        text = p.read_text(errors="replace").splitlines()[line].strip()
    except (OSError, IndexError):
        text = ""
    try:
        shown = p.relative_to(root.resolve()).as_posix()
    except ValueError:
        shown = str(p)
    return {"path": shown, "line": line + 1, "col": rng.get("start", {}).get("character", 0) + 1, "text": text[:200]}


def lookup(root: str, path: str, line: int, col: int, kind: str = "definition", timeout: float = 20) -> dict:
    """kind definition | references | hover at path:line:col (1-based) under repo root."""
    if kind not in METHODS:
        raise CommonsError(f"kind is one of {', '.join(METHODS)}")
    rootp = Path(root)
    f = (rootp / path) if not Path(path).is_absolute() else Path(path)
    lang = EXT.get(f.suffix.lower())
    if not lang or not f.exists():
        raise CommonsError(f"no supported source file at {path}")
    cmd = server_for(lang)
    if not cmd:
        raise CommonsError(f"no language server for {FAMILY.get(lang, lang)} on this machine: install one of "
                           + ", ".join(c[0] for c in SERVERS.get(FAMILY.get(lang, lang), [])))
    c = Client(cmd, rootp, timeout)  # ponytail: one server per lookup; keep them warm if lookups get frequent
    try:
        c.open(f, lang)
        params = {"textDocument": {"uri": _uri(f)}, "position": {"line": max(0, line - 1), "character": max(0, col - 1)}}
        if kind == "references":
            params["context"] = {"includeDeclaration": True}
        res = c.request(METHODS[kind], params)
    finally:
        c.close()
    if kind == "hover":
        body = (res or {}).get("contents") if isinstance(res, dict) else None
        if isinstance(body, dict):
            body = body.get("value")
        elif isinstance(body, list):
            body = "\n".join(x.get("value", "") if isinstance(x, dict) else str(x) for x in body)
        return {"server": cmd[0], "hover": (body or "").strip()}
    locs = res if isinstance(res, list) else ([res] if res else [])
    return {"server": cmd[0], "kind": kind, "results": [_loc(rootp, x) for x in locs][:100]}


def find(s: Store, symbol: str, project: str, kind: str = "definition", timeout: float = 20) -> dict:
    """Resolve a symbol through the code index, then ask the language server about it."""
    pr = s.q("SELECT id, root FROM projects WHERE name=? OR id=?", (project, project))
    if not pr or not pr[0]["root"]:
        raise CommonsError(f"project {project} has no indexed repo root (run `commons index <repo>` first)")
    rows = s.q("SELECT name, path, line FROM entities WHERE project_id=? AND path IS NOT NULL AND line IS NOT NULL AND "
               "(name=? OR name LIKE ?) ORDER BY length(name) LIMIT 1", (pr[0]["id"], symbol, f"%.{symbol}"))
    if not rows:
        raise CommonsError(f"{symbol} isn't in the code index for {project}")
    r = dict(rows[0])
    root = Path(pr[0]["root"])
    try:
        src = (root / r["path"]).read_text(errors="replace").splitlines()[r["line"] - 1]
    except (OSError, IndexError) as e:
        raise CommonsError(f"{r['path']}:{r['line']} isn't readable here") from e
    short = symbol.split(".")[-1]
    col = src.find(short) + 1 if short in src else 1
    return {"symbol": r["name"], "at": f"{r['path']}:{r['line']}:{col}"} | lookup(str(root), r["path"], r["line"], col, kind,
                                                                                   timeout)
