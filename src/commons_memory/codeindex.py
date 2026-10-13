"""Code indexer: files, symbols, imports and calls for Python (ast), JS/TS (tree-sitter when installed —
`pip install commons-memory[code]` — else a lightweight regex parser) and Go, Rust, Java, Ruby, C, C++ and C#
(tree-sitter; a language whose grammar is not installed is skipped).

Call targets are *resolved*: same-file definitions, imported bindings (`from x import f as g`, `import {f as g} from
'./x'`, `const m = require('./x')`, namespace imports), `self.`/`this.` methods of the enclosing class, then a globally
unique name. Each `calls` edge carries meta.resolved = local|import|self|unique|ambiguous.

Writes into the shared graph so code entities link with memories, decisions and sessions.
Entity types: repo(project), file, module, function, class, method.
Relations: contains, defines, imports, calls, inherits.
"""
from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

from .store import Store

PY_EXT = {".py"}
JS_EXT = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
# meta.lang for every indexed extension. JS and TS share "js" (the regex fallback is one parser).
_EXT_LANG = {
    ".py": "python",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js", ".ts": "js", ".tsx": "js",
    ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".cs": "csharp",
}
# Grammar key. ".ts"/".tsx" differ from meta.lang; ".h" is C and ".hh"/".hpp" are C++.
_EXT_TS = {
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js", ".ts": "ts", ".tsx": "tsx",
    ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".cs": "csharp",
}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".tox", ".mypy_cache",
             ".pytest_cache", ".next", "coverage", "site-packages"}
MAX_FILE_BYTES = 512_000


def index_repo(store: Store, root: str, project: str | None = None, agent_id: str | None = None) -> dict:
    rootp = Path(root).resolve()
    project = project or rootp.name
    pid = store.project_id(project, str(rootp))
    stats = {"project": project, "project_id": pid, "files": 0, "symbols": 0, "imports": 0, "calls": 0, "skipped": 0}
    symtab: dict[str, list[str]] = {}       # short name -> entity ids
    pending_calls: list[tuple] = []  # (caller id, callee name, receiver|None, file id, enclosing class|None)
    ctx = {"defs": {}, "bindings": {}, "parser": None}  # per-file top-level defs and import bindings
    modules: dict[str, str] = {}            # module dotted name / rel path -> file id
    pending_imports: list[tuple[str, str]] = []
    with store.lock:
        store.db.execute("BEGIN")
        try:
            kept = _wipe_code_graph(store, pid)  # inside the transaction: a failed re-index keeps the old graph
            for dirpath, dirnames, filenames in os.walk(rootp):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
                for fn in sorted(filenames):
                    path = Path(dirpath) / fn
                    ext = path.suffix.lower()
                    if ext not in _EXT_LANG:
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
                    fid = store.upsert_entity("file", rel, pid, rel, meta={"lang": _EXT_LANG[ext],
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
                        _index_python(store, pid, fid, rel, src, symtab, pending_calls, pending_imports, stats, ctx)
                    elif ext in JS_EXT:
                        if _ts_parser(ext) is not None:
                            _index_js_ts(store, pid, fid, rel, src, ext, symtab, pending_calls, pending_imports, stats, ctx)
                            stats["parser_js"] = "tree-sitter"
                        else:
                            _index_js(store, pid, fid, rel, src, symtab, pending_calls, pending_imports, stats, ctx)
                            stats["parser_js"] = "regex"
                    elif _ts_parser(ext) is not None:
                        _index_ts_lang(store, pid, fid, rel, src, _EXT_TS[ext], symtab, pending_calls, pending_imports, stats, ctx)
            # resolve imports to files where possible, else to module entities
            for fid, target in pending_imports:
                dst = modules.get(target) or modules.get(_resolve_rel(store, fid, target)) or \
                    store.upsert_entity("module", target, pid)
                store.relate(fid, dst, "imports")
                stats["imports"] += 1
            stats["calls_resolved"] = stats["calls_ambiguous"] = 0
            seen_edges = set()
            for caller, name, recv, cfid, cls in pending_calls:
                targets, how = _resolve_call(store, name, recv, cfid, cls, ctx, modules, symtab)
                for callee in targets:
                    if callee != caller and (caller, callee) not in seen_edges:
                        seen_edges.add((caller, callee))
                        store.relate(caller, callee, "calls", meta={"resolved": how})
                        stats["calls"] += 1
                        stats["calls_ambiguous" if how == "ambiguous" else "calls_resolved"] += 1
            stats["relinked"] = _relink(store, pid, kept)
            store.db.execute("COMMIT")
        except Exception:
            store.db.execute("ROLLBACK")
            raise
    import subprocess

    def git(*a):
        try:
            return subprocess.run(["git", "-C", str(rootp), *a], capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    head = git("rev-parse", "--short", "HEAD")  # pin source badges to the commit that was indexed
    if head:
        store.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"codecommit:{pid}", head))
        stats["commit"] = head
        full, origin = git("rev-parse", "HEAD"), git("remote", "get-url", "origin")
        m = re.match(r"(?:git@github\.com:|https://github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?$", origin)
        if full and m:  # commit-pinned links to the exact source on GitHub
            store.x("INSERT OR REPLACE INTO meta VALUES(?,?)",
                    (f"codesource:{pid}", f"https://github.com/{m.group(1)}/blob/{full}/"))
    try:  # file-level architecture snapshot for `diagram delta`
        from .diagrams import snapshot
        snapshot(store, project)
    except Exception:  # noqa: BLE001 - a snapshot problem must not fail the index
        pass
    store.audit(agent_id, "code.index", pid, stats)
    return stats


CODE_RELS = ("contains", "defines", "imports", "calls", "inherits")


def _wipe_code_graph(store: Store, pid: str) -> list[tuple]:
    """Delete the project's code entities and edges for a clean re-index, returning the other edges that touched
    them (a memory that `mentions` a function, a decision `about` a file) keyed by (type, name, path) so _relink
    can re-attach them to the re-created entities."""
    old = {r["id"]: (r["type"], r["name"], r["path"]) for r in store.q(
        "SELECT id, type, name, path FROM entities WHERE project_id=? AND type IN ('file','function','class','method','module')",
        (pid,))}
    ids, kept = list(old), []
    ph_types = ",".join("?" * len(CODE_RELS))
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        ph = ",".join("?" * len(chunk))
        for r in store.q(f"SELECT src, dst, type, meta FROM relations WHERE (src IN ({ph}) OR dst IN ({ph})) "
                         f"AND type NOT IN ({ph_types})", chunk + chunk + list(CODE_RELS)):
            kept.append((old.get(r["src"], r["src"]), old.get(r["dst"], r["dst"]), r["type"], r["meta"]))
        store.x(f"DELETE FROM relations WHERE src IN ({ph}) OR dst IN ({ph})", chunk + chunk)
        store.x(f"DELETE FROM entities WHERE id IN ({ph})", chunk)
    return kept


def _relink(store: Store, pid: str, kept: list[tuple]) -> int:
    n = 0
    for src, dst, typ, meta in kept:
        ends = []
        for e in (src, dst):
            if isinstance(e, tuple):
                r = store.q("SELECT id FROM entities WHERE project_id=? AND type=? AND name=? AND path IS ?", (pid, *e))
                e = r[0]["id"] if r else None
            ends.append(e)
        if all(ends):
            store.relate(ends[0], ends[1], typ, json.loads(meta) if meta else None)
            n += 1
    return n


def _target_file(store, fid, target, modules):
    t = modules.get(target) or modules.get(_resolve_rel(store, fid, target))
    if t:
        return t
    rel = _resolve_rel(store, fid, target)
    for cand in (rel + "/index", rel.rstrip("/") + "/__init__"):
        if cand in modules:
            return modules[cand]
    return None


def _resolve_call(store, name, recv, fid, cls, ctx, modules, symtab):
    defs = ctx["defs"]
    binds = ctx["bindings"].get(fid, {})
    if recv in ("self", "this", "cls") and cls:
        e = defs.get(fid, {}).get(f"{cls}.{name}")
        if e:
            return [e], "self"
    if recv and recv in binds:  # module alias / namespace import: alias.name(...)
        mod, imported = binds[recv]
        tf = _target_file(store, fid, mod, modules)
        if tf:
            e = defs.get(tf, {}).get(name if imported is None else f"{imported}.{name}") or defs.get(tf, {}).get(name)
            if e:
                return [e], "import"
    if recv is None:
        e = defs.get(fid, {}).get(name)
        if e:
            return [e], "local"
        if name in binds:
            mod, imported = binds[name]
            tf = _target_file(store, fid, mod, modules)
            if tf:
                e = defs.get(tf, {}).get(imported or name)
                if e:
                    return [e], "import"
    cands = symtab.get(name, [])
    if len(cands) == 1:
        return cands, "unique"
    if recv not in ("self", "this") and 1 < len(cands) <= 3:
        return cands, "ambiguous"
    return [], "unresolved"


def _resolve_rel(store: Store, fid: str, target: str) -> str:
    if not target.startswith("."):
        return target
    r = store.q("SELECT path FROM entities WHERE id=?", (fid,))
    base = Path(r[0]["path"]).parent if r else Path(".")
    p = os.path.normpath((base / target).as_posix())
    return p


def _index_python(store, pid, fid, rel, src, symtab, pending_calls, pending_imports, stats, ctx):
    defs = ctx["defs"].setdefault(fid, {})
    binds = ctx["bindings"].setdefault(fid, {})
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
                defs.setdefault(qual, eid)
                if not prefix:
                    defs.setdefault(child.name, eid)
                stats["symbols"] += 1
                cls_name = qual if is_class else (prefix[:-1] if in_class else None)
                if is_class:
                    for b in child.bases:
                        bn, br = _py_call(b)
                        if bn:
                            pending_calls.append((eid, bn, br, fid, None))  # inherits ~ depends on
                for sub in _walk_own(child):
                    if isinstance(sub, ast.Call):
                        n, r = _py_call(sub.func)
                        if n:
                            pending_calls.append((eid, n, r, fid, cls_name))
                visit(child, eid, qual + ".", is_class)
            elif isinstance(child, ast.Import):
                for a in child.names:
                    pending_imports.append((fid, a.name))
                    binds[a.asname or a.name.split(".")[0]] = (a.name if a.asname else a.name.split(".")[0], None)
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
                for a in child.names:
                    if a.name != "*":
                        binds[a.asname or a.name] = (mod, a.name)
                        binds.setdefault("__from__" + (a.asname or a.name), (f"{mod}.{a.name}", None))
            elif not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if parent_id == fid:
                    for sub in ast.walk(child):
                        if isinstance(sub, ast.Call):
                            n, r = _py_call(sub.func)
                            if n:
                                pending_calls.append((fid, n, r, fid, None))

    visit(tree, fid, "", False)


def _walk_own(node):
    """ast.walk that does not descend into nested function/class definitions (they record their own calls)."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        n = stack.pop()
        yield n
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(n))


def _py_call(node) -> tuple[str | None, str | None]:
    """(callee name, receiver) — receiver is the simple name before the dot (self, module alias), else None."""
    if isinstance(node, ast.Name):
        return node.id, None
    if isinstance(node, ast.Attribute):
        return node.attr, node.value.id if isinstance(node.value, ast.Name) else "?"
    return None, None


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


def _index_js(store, pid, fid, rel, src, symtab, pending_calls, pending_imports, stats, ctx):
    defs = ctx["defs"].setdefault(fid, {})
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
        defs.setdefault(name, cid)
        if m.group(2):
            pending_calls.append((cid, m.group(2).split(".")[-1], None, fid, None))
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
            defs.setdefault(f"{name}.{mname}", mid)
            stats["symbols"] += 1
            spans.append((mid, mstart, mend, name))
    for rx in (_JS_FUNC, _JS_ARROW):
        for m in rx.finditer(src):
            name = m.group(1)
            end = _block_end(src, m.end()) if "{" in src[m.end():m.end() + 200] else src.find("\n", m.end())
            eid = store.upsert_entity("function", name, pid, rel, line_of(m.start()))
            store.relate(fid, eid, "defines")
            symtab.setdefault(name, []).append(eid)
            defs.setdefault(name, eid)
            stats["symbols"] += 1
            spans.append((eid, m.end(), end if end > 0 else len(src), None))
    for eid, s, e, cls in spans:
        for c in _JS_CALL.finditer(src[s:e]):
            if c.group(1) not in _JS_KEYWORDS:
                before = src[s:e][max(0, c.start() - 6):c.start()]
                recv = "this" if before.endswith("this.") else ("?" if before.endswith(".") else None)
                pending_calls.append((eid, c.group(1), recv, fid, cls))


# ---------------------------------------------------------------------------- tree-sitter JS/TS
_TS_CACHE: dict[str, object] = {}


_TS_GRAMMAR = {
    "js": ("tree_sitter_javascript", "language"),
    "ts": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "go": ("tree_sitter_go", "language"),
    "rust": ("tree_sitter_rust", "language"),
    "java": ("tree_sitter_java", "language"),
    "ruby": ("tree_sitter_ruby", "language"),
    "c": ("tree_sitter_c", "language"),
    "cpp": ("tree_sitter_cpp", "language"),
    "csharp": ("tree_sitter_c_sharp", "language"),
}


def _ts_parser(ext: str):
    if os.environ.get("COMMONS_TREE_SITTER", "1") != "1":
        return None
    key = _EXT_TS.get(ext)
    if key is None:
        return None
    if key in _TS_CACHE:
        return _TS_CACHE[key]
    try:
        from tree_sitter import Language, Parser
        mod_name, fn = _TS_GRAMMAR[key]
        mod = __import__(mod_name)
        _TS_CACHE[key] = Parser(Language(getattr(mod, fn)()))
    except Exception:  # noqa: BLE001 - optional dependency, or the grammar package is not installed
        _TS_CACHE[key] = None
    return _TS_CACHE[key]


def _t(n) -> str:
    return n.text.decode("utf-8", "replace") if n is not None else ""


def _index_js_ts(store, pid, fid, rel, src, ext, symtab, pending_calls, pending_imports, stats, ctx):
    tree = _ts_parser(ext).parse(src.encode())
    defs = ctx["defs"].setdefault(fid, {})
    binds = ctx["bindings"].setdefault(fid, {})

    def define(typ, name, node, parent, meta=None):
        eid = store.upsert_entity(typ, name, pid, rel, node.start_point[0] + 1,
                                  (meta or {}) | {"end_line": node.end_point[0] + 1})
        store.relate(parent, eid, "defines")
        symtab.setdefault(name.split(".")[-1], []).append(eid)
        defs.setdefault(name, eid)
        stats["symbols"] += 1
        return eid

    def calls_in(node, owner, cls):
        stack = list(node.children)
        while stack:
            n = stack.pop()
            if n.type in ("function_declaration", "class_declaration", "method_definition", "arrow_function",
                          "function_expression", "generator_function_declaration") and n is not node:
                if n.type in ("arrow_function", "function_expression") and n.parent is not None \
                        and n.parent.type != "variable_declarator":
                    pass  # inline callbacks: their calls belong to the enclosing function
                else:
                    continue
            if n.type == "call_expression":
                f = n.child_by_field_name("function")
                if f is not None and f.type == "identifier" and _t(f) not in _JS_KEYWORDS:
                    pending_calls.append((owner, _t(f), None, fid, cls))
                elif f is not None and f.type == "member_expression":
                    obj, prop = f.child_by_field_name("object"), f.child_by_field_name("property")
                    recv = "this" if obj is not None and obj.type == "this" else (_t(obj) if obj is not None and obj.type == "identifier" else "?")
                    pending_calls.append((owner, _t(prop), recv, fid, cls))
            elif n.type == "new_expression":
                c = n.child_by_field_name("constructor")
                if c is not None and c.type == "identifier":
                    pending_calls.append((owner, _t(c), None, fid, cls))
            stack.extend(n.children)

    def handle_import(n):
        srcn = n.child_by_field_name("source")
        mod = _t(srcn).strip("'\"`") if srcn is not None else ""
        if not mod:
            return
        pending_imports.append((fid, mod))
        for c in n.children:
            if c.type != "import_clause":
                continue
            for cc in c.children:
                if cc.type == "identifier":
                    binds[_t(cc)] = (mod, "default")
                elif cc.type == "namespace_import":
                    ids = [x for x in cc.children if x.type == "identifier"]
                    if ids:
                        binds[_t(ids[-1])] = (mod, None)
                elif cc.type == "named_imports":
                    for sp in cc.children:
                        if sp.type == "import_specifier":
                            nm, al = sp.child_by_field_name("name"), sp.child_by_field_name("alias")
                            binds[_t(al or nm)] = (mod, _t(nm))

    def walk(node, parent, cls=None):
        for n in node.children:
            t = n.type
            if t == "export_statement":
                walk(n, parent, cls)
                if n.child_by_field_name("source") is not None:
                    handle_import(n)
            elif t == "import_statement":
                handle_import(n)
            elif t in ("function_declaration", "generator_function_declaration"):
                name = _t(n.child_by_field_name("name"))
                params = _t(n.child_by_field_name("parameters"))
                eid = define("function", name, n, parent, {"signature": params[:120]})
                calls_in(n, eid, None)
            elif t in ("class_declaration", "abstract_class_declaration", "class"):
                name = _t(n.child_by_field_name("name")) or "anonymous"
                ext_name = None
                for c in n.children:
                    if c.type == "class_heritage":
                        ids = [x for x in c.children if x.type in ("identifier", "extends_clause")]
                        for x in ids:
                            v = x.child_by_field_name("value") if x.type == "extends_clause" else x
                            ext_name = _t(v) if v is not None else _t(x)
                            break
                        if ext_name is None:
                            ext_name = _t(c).replace("extends", "").strip().split()[0] if _t(c) else None
                cid = define("class", name, n, parent, {"extends": ext_name})
                if ext_name:
                    pending_calls.append((cid, ext_name.split(".")[-1], None, fid, None))
                body = n.child_by_field_name("body")
                for m in (body.children if body is not None else []):
                    if m.type in ("method_definition", "method_signature", "abstract_method_signature"):
                        mn = _t(m.child_by_field_name("name"))
                        if mn and mn != "constructor" or m.type == "method_definition":
                            mid = define("method", f"{name}.{mn}", m, cid,
                                         {"signature": _t(m.child_by_field_name("parameters"))[:120]})
                            calls_in(m, mid, name)
            elif t in ("lexical_declaration", "variable_declaration"):
                for d in n.children:
                    if d.type != "variable_declarator":
                        continue
                    nm, val = d.child_by_field_name("name"), d.child_by_field_name("value")
                    if val is None or nm is None:
                        continue
                    if val.type in ("arrow_function", "function_expression", "function"):
                        eid = define("function", _t(nm), d, parent, {"signature": _t(val.child_by_field_name("parameters"))[:120]})
                        calls_in(val, eid, None)
                    elif val.type == "call_expression" and _t(val.child_by_field_name("function")) == "require":
                        args = val.child_by_field_name("arguments")
                        strs = [x for x in (args.children if args is not None else []) if x.type == "string"]
                        if strs:
                            mod = _t(strs[0]).strip("'\"`")
                            pending_imports.append((fid, mod))
                            if nm.type == "identifier":
                                binds[_t(nm)] = (mod, None)
                            elif nm.type == "object_pattern":
                                for p_ in nm.children:
                                    if p_.type in ("shorthand_property_identifier_pattern", "identifier"):
                                        binds[_t(p_)] = (mod, _t(p_))
                    elif parent == fid:
                        calls_in(d, fid, None)
            elif t == "expression_statement" and parent == fid:
                calls_in(n, fid, None)

    walk(tree.root_node, fid)
    # default export binding: `export default function x` / `export default X`
    for n in tree.root_node.children:
        if n.type == "export_statement" and any(c.type == "default" for c in n.children):
            for c in n.children:
                nm = c.child_by_field_name("name") if c.child_count else None
                if nm is not None and _t(nm) in defs:
                    defs.setdefault("default", defs[_t(nm)])
                elif c.type == "identifier" and _t(c) in defs:
                    defs.setdefault("default", defs[_t(c)])


# ---------------------------------------------------------------------------- tree-sitter (Go, Rust, Java, Ruby, C, C++, C#)
# One walker. The table is node types; names, calls and imports differ enough that each language has a small extractor.
_TS_LANGS = {
    "go": {"containers": frozenset({"type_spec"}),
           "functions": frozenset({"function_declaration", "method_declaration", "method_elem"}),
           "calls": frozenset({"call_expression"}), "imports": frozenset({"import_spec"})},
    "rust": {"containers": frozenset({"struct_item", "enum_item", "union_item", "trait_item", "impl_item"}),
             "functions": frozenset({"function_item"}),
             "calls": frozenset({"call_expression"}), "imports": frozenset({"use_declaration"})},
    "java": {"containers": frozenset({"class_declaration", "interface_declaration", "enum_declaration",
                                      "record_declaration", "annotation_type_declaration"}),
             "functions": frozenset({"method_declaration", "constructor_declaration"}),
             "calls": frozenset({"method_invocation", "object_creation_expression"}),
             "imports": frozenset({"import_declaration"})},
    "ruby": {"containers": frozenset({"class", "module", "singleton_class"}),
             "functions": frozenset({"method", "singleton_method"}),
             "calls": frozenset({"call", "command", "command_call"}), "imports": frozenset()},
    "c": {"containers": frozenset({"struct_specifier", "union_specifier", "enum_specifier"}),
          "functions": frozenset({"function_definition"}),
          "calls": frozenset({"call_expression"}), "imports": frozenset({"preproc_include"})},
    "cpp": {"containers": frozenset({"struct_specifier", "union_specifier", "enum_specifier", "class_specifier"}),
            "functions": frozenset({"function_definition"}),
            "calls": frozenset({"call_expression"}), "imports": frozenset({"preproc_include"})},
    "csharp": {"containers": frozenset({"class_declaration", "interface_declaration", "struct_declaration",
                                        "enum_declaration", "record_declaration"}),
               "functions": frozenset({"method_declaration", "constructor_declaration", "local_function_statement"}),
               "calls": frozenset({"invocation_expression"}), "imports": frozenset({"using_directive"})},
}
_TYPE_NODES = {"type_identifier", "identifier", "namespace_identifier", "constant"}
_SIMPLE_RECV = {"identifier", "type_identifier", "this", "self", "super"}
_DECL_WRAP = {"pointer_declarator", "reference_declarator", "parenthesized_declarator"}


def _ok_name(name: str) -> bool:
    return bool(name) and "\n" not in name and "{" not in name and len(name) < 200


def _bare_type(node) -> str:
    """Innermost type name: `*Server`, `Foo<T>`, `Box<int>` and `pkg.Server` all yield the type itself."""
    if node is None:
        return ""
    if node.type in _TYPE_NODES:
        return _t(node)
    if node.type in ("generic_type", "template_type"):
        return _bare_type(node.child_by_field_name("type") or node.child_by_field_name("name"))
    if node.type in ("scoped_identifier", "qualified_identifier", "scoped_type_identifier", "qualified_type"):
        return _bare_type(node.child_by_field_name("name"))
    named = node.child_by_field_name("name")
    if named is not None and named.type in _TYPE_NODES:
        return _t(named)
    for c in node.children:
        if c.is_named:
            got = _bare_type(c)
            if got:
                return got
    return ""


def _recv(node) -> str:
    """Method-call receiver: a bare identifier (or self/this), otherwise '?'."""
    if node is not None and node.type in _SIMPLE_RECV:
        return _t(node)
    return "?"


def _string_content(node) -> str:
    if node is None:
        return ""
    if node.type == "system_lib_string":
        return _t(node).strip("<> \t")
    if node.type in ("string_content", "interpreted_string_literal_content", "raw_string_literal_content"):
        return _t(node)
    for c in node.children:
        got = _string_content(c)
        if got:
            return got
    return ""


def _container_name(lang: str, node) -> str:
    if lang == "go":
        kind = node.child_by_field_name("type")
        if kind is None or kind.type not in ("struct_type", "interface_type"):
            return ""
        return _t(node.child_by_field_name("name"))
    if lang == "rust" and node.type == "impl_item":
        return _bare_type(node.child_by_field_name("type"))
    if lang in ("c", "cpp") and node.child_by_field_name("body") is None:
        return ""  # `struct Server *s` is a reference, not a definition
    return _t(node.child_by_field_name("name"))


def _func_parts(lang: str, node) -> tuple[str, str | None]:
    """(name, enclosing type from the declaration itself). The type is set for Go receivers and C++ `Type::method`."""
    if lang == "go" and node.type == "method_declaration":
        recv = node.child_by_field_name("receiver")
        typ = ""
        for c in recv.children if recv is not None else []:
            if c.type == "parameter_declaration":
                typ = _bare_type(c.child_by_field_name("type"))
                break
        return _t(node.child_by_field_name("name")), typ or None
    if lang in ("c", "cpp") and node.type == "function_definition":
        return _c_func_name(node)
    return _t(node.child_by_field_name("name")), None


def _c_func_name(node) -> tuple[str, str | None]:
    decl = node.child_by_field_name("declarator")
    while decl is not None and decl.type in _DECL_WRAP:
        decl = decl.child_by_field_name("declarator")
    if decl is None or decl.type != "function_declarator":
        return "", None
    inner = decl.child_by_field_name("declarator")
    while inner is not None and inner.type in _DECL_WRAP:
        inner = inner.child_by_field_name("declarator")
    if inner is None:
        return "", None
    if inner.type == "destructor_name":
        ident = next((c for c in inner.children if c.type == "identifier"), None)
        return ("~" + _t(ident)) if ident is not None else "", None
    if inner.type in ("qualified_identifier", "scoped_identifier"):
        scope = _bare_type(inner.child_by_field_name("scope"))
        return _t(inner.child_by_field_name("name")), scope or None
    if inner.type == "operator_name":
        return _t(inner).replace(" ", ""), None
    if inner.type in ("identifier", "field_identifier"):
        return _t(inner), None
    return _t(inner), None


def _call_parts(lang: str, node) -> tuple[str, str | None]:
    if lang == "java":
        if node.type == "object_creation_expression":
            return _bare_type(node.child_by_field_name("type")), None
        name = node.child_by_field_name("name")
        obj = node.child_by_field_name("object")
        return (_t(name), None if obj is None else _recv(obj)) if name is not None else ("", None)
    if lang == "csharp":
        f = node.child_by_field_name("function")
        if f is None:
            return "", None
        if f.type == "identifier":
            return _t(f), None
        if f.type == "member_access_expression":
            return _t(f.child_by_field_name("name")), _recv(f.child_by_field_name("expression"))
        return "", None
    if lang == "ruby":
        method = node.child_by_field_name("method")
        if method is None or _t(method) in ("require", "require_relative"):
            return "", None
        recv = node.child_by_field_name("receiver")
        return _t(method), None if recv is None else _recv(recv)
    f = node.child_by_field_name("function")
    if f is None:
        return "", None
    if f.type == "identifier":
        return _t(f), None
    if f.type == "selector_expression":  # Go: fmt.Println
        return _t(f.child_by_field_name("field")), _recv(f.child_by_field_name("operand"))
    if f.type == "field_expression":  # Rust obj.m, C/C++ obj.m / obj->m
        obj = f.child_by_field_name("value") or f.child_by_field_name("argument")
        return _t(f.child_by_field_name("field")), _recv(obj)
    if f.type in ("scoped_identifier", "qualified_identifier"):
        path = f.child_by_field_name("path") or f.child_by_field_name("scope")
        return _t(f.child_by_field_name("name")), _recv(path)
    if f.type == "generic_function":
        inner = f.child_by_field_name("function")
        if inner is not None and inner.type == "identifier":
            return _t(inner), None
    return "", None


def _import_module(lang: str, node) -> str:
    if lang == "ruby":
        if node.type not in ("call", "command", "command_call"):
            return ""
        method = node.child_by_field_name("method")
        if method is None or _t(method) not in ("require", "require_relative"):
            return ""
        return _string_content(node.child_by_field_name("arguments"))
    if node.type not in _TS_LANGS[lang]["imports"]:
        return ""
    if lang == "go":
        return _string_content(node.child_by_field_name("path"))
    if lang == "rust":
        arg = node.child_by_field_name("argument")
        return " ".join(_t(arg).split()) if arg is not None else ""
    if lang == "java":
        for c in node.children:
            if c.type in ("scoped_identifier", "identifier"):
                return _t(c)
        return ""
    if lang == "csharp":
        quals = [c for c in node.children if c.type == "qualified_name"]
        if quals:
            return _t(quals[-1])
        ids = [c for c in node.named_children if c.type == "identifier"]
        return _t(ids[-1]) if ids else ""
    if lang in ("c", "cpp"):
        return _string_content(node.child_by_field_name("path"))
    return ""


def _index_ts_lang(store, pid, fid, rel, src, lang, symtab, pending_calls, pending_imports, stats, ctx):
    spec = _TS_LANGS.get(lang)
    parser = _ts_parser_key(lang)
    if spec is None or parser is None:
        return
    tree = parser.parse(src.encode())
    defs = ctx["defs"].setdefault(fid, {})
    defined: set[str] = set()
    class_by_name: dict[str, str] = {}
    func_types, cont_types, call_types = spec["functions"], spec["containers"], spec["calls"]

    def define(typ, name, node, parent):
        if not _ok_name(name) or name in defined:
            return defs.get(name)
        eid = store.upsert_entity(typ, name, pid, rel, node.start_point[0] + 1, {"end_line": node.end_point[0] + 1})
        store.relate(parent, eid, "defines")
        symtab.setdefault(name.split(".")[-1], []).append(eid)
        defs[name] = eid
        defined.add(name)
        short = name.split(".")[-1]
        if short != name:  # bare `helper()` in the same file (Java, C#, C++ methods) resolves locally
            defs.setdefault(short, eid)
        stats["symbols"] += 1
        return eid

    def containers(node, parent_eid):
        if node.type in cont_types:
            name = _container_name(lang, node)
            if name:
                cid = define("class", name, node, parent_eid)
                if cid:
                    class_by_name.setdefault(name, cid)
                    for c in node.children:
                        containers(c, cid)
                    return
        for c in node.children:
            containers(c, parent_eid)

    def calls_in(node, owner, cls):
        stack = list(node.children)
        while stack:
            n = stack.pop()
            if n.type in func_types or n.type in cont_types:
                continue  # nested function or type owns its own calls
            if n.type in call_types:
                name, recv = _call_parts(lang, n)
                if _ok_name(name):
                    pending_calls.append((owner, name, recv, fid, cls))
            stack.extend(n.children)

    def walk(node, parent_eid, container, member):
        # tree-sitter Node wrappers are not stable under id(), so a container is recognized by its type
        if node.type in cont_types:
            name = _container_name(lang, node)
            if name:
                cid = class_by_name.get(name, parent_eid)
                for c in node.children:
                    walk(c, cid, name, True)
                return
        if node.type in func_types:
            fname, override = _func_parts(lang, node)
            if not _ok_name(fname):
                return
            if override:
                qual, typ, cls, parent = f"{override}.{fname}", "method", override, class_by_name.get(override, parent_eid)
            elif member and container:
                qual, typ, cls, parent = f"{container}.{fname}", "method", container, parent_eid
            else:
                qual, typ, cls, parent = fname, "function", None, parent_eid
            eid = define(typ, qual, node, parent)
            if eid:
                calls_in(node, eid, cls)
                for c in node.children:
                    walk(c, eid, None, False)
            return
        mod = _import_module(lang, node)
        if mod:
            pending_imports.append((fid, mod))
            if lang == "ruby":  # require "x" do def inner; end end — the block is a child of the call
                for c in node.children:
                    walk(c, parent_eid, container, member)
            return
        for c in node.children:
            walk(c, parent_eid, container, member)

    containers(tree.root_node, fid)
    walk(tree.root_node, fid, None, False)


def _ts_parser_key(lang: str):
    """Parser for a grammar key. `_ts_parser` is keyed by extension; several extensions share one grammar."""
    for ext, key in _EXT_TS.items():
        if key == lang:
            return _ts_parser(ext)
    return None


def code_symbols(store: Store, name: str = "", project: str | None = None, types: list[str] | None = None,
                 path: str | None = None, limit: int = 50) -> list[dict]:
    where, args = ["type IN ('function','class','method')"], []
    if types:
        where = [f"type IN ({','.join('?' * len(types))})"]
        args += types
    if name:
        where.append("(name LIKE ? OR name LIKE ?)"); args += [f"%{name}%", f"%.{name}"]
    if project:
        where.append("project_id=?"); args.append(store.find_project(project) or "__none__")
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
