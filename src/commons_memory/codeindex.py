"""Code indexer: files, symbols, imports and calls for Python (ast) and JS/TS (lightweight parser).

Writes into the shared graph so code entities link with memories, decisions and sessions.
Entity types: repo(project), file, module, function, class, method.
Relations: contains, defines, imports, calls, inherits.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from .store import Store

PY_EXT = {".py"}
JS_EXT = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".tox", ".mypy_cache",
             ".pytest_cache", ".next", "coverage", "site-packages"}
MAX_FILE_BYTES = 512_000


def index_repo(store: Store, root: str, project: str | None = None, agent_id: str | None = None) -> dict:
    rootp = Path(root).resolve()
    project = project or rootp.name
    pid = store.project_id(project, str(rootp))
    stats = {"project": project, "project_id": pid, "files": 0, "symbols": 0, "imports": 0, "calls": 0, "skipped": 0}
    # wipe previous code graph for this project (idempotent re-index)
    old = [r["id"] for r in store.q("SELECT id FROM entities WHERE project_id=? AND type IN "
                                    "('file','function','class','method','module')", (pid,))]
    for i in range(0, len(old), 500):
        chunk = old[i:i + 500]
        ph = ",".join("?" * len(chunk))
        store.x(f"DELETE FROM relations WHERE src IN ({ph}) OR dst IN ({ph})", chunk + chunk)
        store.x(f"DELETE FROM entities WHERE id IN ({ph})", chunk)

    symtab: dict[str, list[str]] = {}       # short name -> entity ids
    pending_calls: list[tuple[str, str]] = []  # (caller entity id, callee short name)
    modules: dict[str, str] = {}            # module dotted name / rel path -> file id
    pending_imports: list[tuple[str, str]] = []
    with store.lock:
        store.db.execute("BEGIN")
        try:
            for dirpath, dirnames, filenames in os.walk(rootp):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
                for fn in sorted(filenames):
                    path = Path(dirpath) / fn
                    ext = path.suffix.lower()
                    if ext not in PY_EXT | JS_EXT:
                        continue
                    if path.stat().st_size > MAX_FILE_BYTES:
                        stats["skipped"] += 1
                        continue
                    rel = path.relative_to(rootp).as_posix()
                    try:
                        src = path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        stats["skipped"] += 1
                        continue
                    fid = store.upsert_entity("file", rel, pid, rel, meta={"lang": "python" if ext in PY_EXT else "js",
                                                                            "lines": src.count("\n") + 1})
                    store.relate(pid, fid, "contains")
                    stats["files"] += 1
                    modules[rel] = fid
                    modules[re.sub(r"\.[^.]+$", "", rel)] = fid
                    if ext in PY_EXT:
                        mod = re.sub(r"(/__init__)?\.py$", "", rel).replace("/", ".")
                        modules[mod] = fid
                        for part in ("src.",):
                            if mod.startswith(part):
                                modules[mod[len(part):]] = fid
                        _index_python(store, pid, fid, rel, src, symtab, pending_calls, pending_imports, stats)
                    else:
                        _index_js(store, pid, fid, rel, src, symtab, pending_calls, pending_imports, stats)
            # resolve imports to files where possible, else to module entities
            for fid, target in pending_imports:
                dst = modules.get(target) or modules.get(_resolve_rel(store, fid, target)) or \
                    store.upsert_entity("module", target, pid)
                store.relate(fid, dst, "imports")
                stats["imports"] += 1
            for caller, name in pending_calls:
                for callee in symtab.get(name, [])[:3]:
                    if callee != caller:
                        store.relate(caller, callee, "calls")
                        stats["calls"] += 1
            store.db.execute("COMMIT")
        except Exception:
            store.db.execute("ROLLBACK")
            raise
    store.audit(agent_id, "code.index", pid, stats)
    return stats


def _resolve_rel(store: Store, fid: str, target: str) -> str:
    if not target.startswith("."):
        return target
    r = store.q("SELECT path FROM entities WHERE id=?", (fid,))
    base = Path(r[0]["path"]).parent if r else Path(".")
    p = os.path.normpath((base / target).as_posix())
    return p


def _index_python(store, pid, fid, rel, src, symtab, pending_calls, pending_imports, stats):
    try:
        tree = ast.parse(src)
    except SyntaxError:
        stats["skipped"] += 1
        return

    def visit(node, parent_id, prefix, in_class):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                is_class = isinstance(child, ast.ClassDef)
                typ = "class" if is_class else ("method" if in_class else "function")
                qual = f"{prefix}{child.name}"
                doc = (ast.get_docstring(child) or "").split("\n")[0][:160]
                sig = ""
                if not is_class:
                    sig = f"({', '.join(a.arg for a in child.args.args)})"
                eid = store.upsert_entity(typ, qual, pid, rel, child.lineno,
                                          {"doc": doc, "signature": sig, "end_line": getattr(child, "end_lineno", None)})
                store.relate(parent_id, eid, "defines")
                symtab.setdefault(child.name, []).append(eid)
                stats["symbols"] += 1
                if is_class:
                    for b in child.bases:
                        bn = _py_name(b)
                        if bn:
                            pending_calls.append((eid, bn.split(".")[-1]))  # inherits ~ depends on
                for sub in ast.walk(child):
                    if isinstance(sub, ast.Call):
                        n = _py_name(sub.func)
                        if n:
                            pending_calls.append((eid, n.split(".")[-1]))
                visit(child, eid, qual + ".", is_class)
            elif isinstance(child, ast.Import):
                for a in child.names:
                    pending_imports.append((fid, a.name))
            elif isinstance(child, ast.ImportFrom):
                mod = ("." * child.level) + (child.module or "")
                if child.level:
                    base = rel.rsplit("/", 1)[0].replace("/", ".") if "/" in rel else ""
                    parts = base.split(".") if base else []
                    parts = parts[: len(parts) - (child.level - 1)] if child.level > 1 else parts
                    mod = ".".join(parts + ([child.module] if child.module else []))
                    if mod.startswith("src."):
                        mod = mod[4:]
                pending_imports.append((fid, mod))
            elif not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if parent_id == fid:
                    for sub in ast.walk(child):
                        if isinstance(sub, ast.Call):
                            n = _py_name(sub.func)
                            if n:
                                pending_calls.append((fid, n.split(".")[-1]))

    visit(tree, fid, "", False)


def _py_name(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


_JS_IMPORT = re.compile(r"""(?:import\s[^'"]*?from\s*|import\s*\(\s*|require\s*\(\s*|import\s+)['"]([^'"]+)['"]""")
_JS_FUNC = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(", re.M)
_JS_ARROW = re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>", re.M)
_JS_CLASS = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)(?:\s+extends\s+([A-Za-z_$][\w$.]*))?", re.M)
_JS_METHOD = re.compile(r"^\s+(?:public\s+|private\s+|protected\s+|static\s+|async\s+|readonly\s+)*([A-Za-z_$][\w$]*)\s*\([^;{]*\)\s*(?::\s*[^{;]+)?\{", re.M)
_JS_CALL = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
_JS_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "typeof", "new", "super", "constructor",
                "require", "import", "await", "async", "yield", "delete", "void", "with", "else"}


def _block_end(src: str, start: int) -> int:
    i = src.find("{", start)
    if i < 0:
        return len(src)
    depth = 0
    for j in range(i, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1
    return len(src)


def _index_js(store, pid, fid, rel, src, symtab, pending_calls, pending_imports, stats):
    line_of = lambda pos: src.count("\n", 0, pos) + 1  # noqa: E731
    for m in _JS_IMPORT.finditer(src):
        pending_imports.append((fid, m.group(1)))
    spans = []
    for m in _JS_CLASS.finditer(src):
        name = m.group(1)
        end = _block_end(src, m.end())
        cid = store.upsert_entity("class", name, pid, rel, line_of(m.start()), {"extends": m.group(2)})
        store.relate(fid, cid, "defines")
        symtab.setdefault(name, []).append(cid)
        stats["symbols"] += 1
        if m.group(2):
            pending_calls.append((cid, m.group(2).split(".")[-1]))
        body = src[m.end():end]
        for mm in _JS_METHOD.finditer(body):
            mname = mm.group(1)
            if mname in _JS_KEYWORDS:
                continue
            mstart = m.end() + mm.start()
            mend = _block_end(src, m.end() + mm.end() - 1)
            mid = store.upsert_entity("method", f"{name}.{mname}", pid, rel, line_of(mstart))
            store.relate(cid, mid, "defines")
            symtab.setdefault(mname, []).append(mid)
            stats["symbols"] += 1
            spans.append((mid, mstart, mend))
    for rx in (_JS_FUNC, _JS_ARROW):
        for m in rx.finditer(src):
            name = m.group(1)
            end = _block_end(src, m.end()) if "{" in src[m.end():m.end() + 200] else src.find("\n", m.end())
            eid = store.upsert_entity("function", name, pid, rel, line_of(m.start()))
            store.relate(fid, eid, "defines")
            symtab.setdefault(name, []).append(eid)
            stats["symbols"] += 1
            spans.append((eid, m.end(), end if end > 0 else len(src)))
    for eid, s, e in spans:
        for c in _JS_CALL.finditer(src[s:e]):
            if c.group(1) not in _JS_KEYWORDS:
                pending_calls.append((eid, c.group(1)))


def code_symbols(store: Store, name: str = "", project: str | None = None, types: list[str] | None = None,
                 path: str | None = None, limit: int = 50) -> list[dict]:
    where, args = ["type IN ('function','class','method')"], []
    if types:
        where = [f"type IN ({','.join('?' * len(types))})"]
        args += types
    if name:
        where.append("(name LIKE ? OR name LIKE ?)"); args += [f"%{name}%", f"%.{name}"]
    if project:
        where.append("project_id=?"); args.append(store.project_id(project))
    if path:
        where.append("path LIKE ?"); args.append(f"%{path}%")
    rows = store.q(f"SELECT id,type,name,path,line,meta FROM entities WHERE {' AND '.join(where)} "
                   f"ORDER BY (name=?) DESC, length(name) LIMIT ?", args + [name, limit])
    import json
    out = []
    for r in rows:
        m = json.loads(r["meta"] or "{}")
        out.append({"id": r["id"], "type": r["type"], "name": r["name"], "path": r["path"], "line": r["line"],
                    "signature": m.get("signature", ""), "doc": m.get("doc", "")})
    return out


def code_impact(store: Store, symbol: str, project: str | None = None, depth: int = 3, limit: int = 100) -> dict:
    """Reverse dependency walk: what calls/imports/inherits this symbol (transitively)."""
    if store.q("SELECT 1 FROM entities WHERE id=?", (symbol,)):
        roots = [symbol]
    else:
        roots = [s["id"] for s in code_symbols(store, symbol, project, limit=10)
                 if s["name"] == symbol or s["name"].endswith("." + symbol)]
        if not roots:
            roots = [r["id"] for r in store.q("SELECT id FROM entities WHERE type='file' AND path LIKE ?", (f"%{symbol}",))]
    seen, frontier, impacted = set(roots), list(roots), []
    for d in range(1, depth + 1):
        nxt = []
        for node in frontier:
            for r in store.q("SELECT src, type FROM relations WHERE dst=? AND type IN ('calls','imports')", (node,)):
                if r["src"] in seen:
                    continue
                seen.add(r["src"])
                nxt.append(r["src"])
                e = store.q("SELECT id,type,name,path,line FROM entities WHERE id=?", (r["src"],))
                if e:
                    impacted.append(dict(e[0]) | {"depth": d, "via": r["type"]})
            # a symbol is also impacted through its file's importers
            if d == 1:
                for r in store.q("SELECT src FROM relations WHERE dst=? AND type='defines'", (node,)):
                    if r["src"] not in seen:
                        seen.add(r["src"]); nxt.append(r["src"])
        frontier = nxt
        if len(impacted) >= limit:
            break
    files = sorted({i["path"] for i in impacted if i.get("path")})
    linked = []
    for root in roots:
        linked += [r["src"] for r in store.q("SELECT src FROM relations r JOIN items i ON i.id=r.src WHERE r.dst=?", (root,))]
    return {"roots": roots, "impacted": impacted[:limit], "files": files, "linked_memories": linked,
            "text": "\n".join(f"{'  ' * (i['depth'] - 1)}{i['name']} ({i['type']}, {i['path']}:{i['line']}) via {i['via']}"
                              for i in impacted[:limit]) or "(no dependents found)"}
