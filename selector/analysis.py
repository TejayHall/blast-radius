"""Static change analysis: patch -> sound candidate set of tests that could fail.

Soundness argument (per changed region), against a per-test coverage map recorded by running every
test in isolation on the pinned commit:
  * A change inside a function body can only alter a test that executes the enclosing statement
    (execution is identical up to the first time the statement runs). We use the statement's whole
    line span (and, for compound statements, the header span), so short-circuit operands and
    multi-line expressions are covered.
  * A change to module/class level code runs at import. Its effect flows through the names it binds.
    The candidate set is the tests that import the module (ran the statement) intersected with the
    tests that execute any runtime line mentioning a bound name. If a bound name is read by code that
    itself runs at import time (metaclasses, class bodies), we stop narrowing and keep every importer.
  * Python changes whose AST is identical (comments, formatting) cannot change behaviour.
  * Non-Python files affect only the tests that opened them (audit-hook record), plus tests that
    listed the directory when files are added or removed.
Anything we cannot analyse falls back to a broader sound set (all importers, or all tests).
"""
import ast
import os
import re

from .patchlib import apply_filediff, changed_regions, changed_regions_detailed, parse_patch
from .pyscope import locate

IDENT_RE_CACHE = {}
_CODING_RE = re.compile(r"^[ 	]*#.*?coding[:=][ 	]*([-\w.]+)")


class FileChange:
    __slots__ = ("path", "old", "new", "kind", "notes")

    def __init__(self, path, old, new, kind):
        self.path, self.old, self.new, self.kind = path, old, new, kind
        self.notes = []


class CommitAnalysis:
    def __init__(self, cid):
        self.cid = cid
        self.files = {}          # relpath -> new text (None = deleted); overlay for the engine
        self.cand = set()        # candidate test indices (sound superset of failing tests)
        self.kinds = set()       # 'py_fn', 'py_top', 'py_noop', 'py_syntax', 'fixture', 'other', 'fallback'
        self.notes = []
        self.verifiable = True   # engine can apply this patch
        self.py_changed = set()  # python files with semantic changes
        self.import_time = False  # any change executes at import time
        self.needs_diff = False   # differential import would sharpen the candidate set
        self.fn_spans = []        # (path, a, b) of changed function-body statements (old coordinates)
        self.diff_fallback = set()  # sound candidates to add if no differential import is run


def _read(root, rel):
    with open(os.path.join(root, rel), encoding="utf-8", newline="") as fh:
        return fh.read()


class Analyzer:
    def __init__(self, index, root):
        self.ix = index
        self.root = root
        self._src_cache = {}
        self._all_py = None

    # ---- textual identifier search over python sources (for name narrowing) ----
    def _py_sources(self):
        if self._all_py is None:
            out = []
            for sub in ("sqlglot", "tests"):
                base = os.path.join(self.root, sub)
                for dp, dn, fns in os.walk(base):
                    for fn in fns:
                        if fn.endswith(".py"):
                            p = os.path.join(dp, fn)
                            rel = os.path.relpath(p, self.root).replace(os.sep, "/")
                            out.append(rel)
            self._all_py = sorted(out)
        return self._all_py

    def _text(self, rel):
        t = self._src_cache.get(rel)
        if t is None:
            try:
                t = _read(self.root, rel)
            except OSError:
                t = ""
            self._src_cache[rel] = t
        return t

    def name_lines(self, name):
        """All (file, line) where identifier `name` occurs as a word (code or strings)."""
        rx = IDENT_RE_CACHE.get(name)
        if rx is None:
            rx = IDENT_RE_CACHE[name] = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")
        out = []
        for rel in self._py_sources():
            txt = self._text(rel)
            if name not in txt:
                continue
            for m in rx.finditer(txt):
                ln = txt.count("\n", 0, m.start()) + 1
                out.append((rel, ln))
        return out

    # ---- main entry ----
    def analyze(self, cid, text):
        ca = CommitAnalysis(cid)
        try:
            fds = parse_patch(text)
        except Exception as e:  # noqa: BLE001
            ca.kinds.add("fallback")
            ca.notes.append(f"unparseable patch: {e!r}")
            ca.cand = self.ix.all_tests()
            ca.verifiable = False
            return ca
        if not fds and text.strip():
            ca.kinds.add("fallback")
            ca.cand = self.ix.all_tests()
            ca.verifiable = False
            return ca
        for fd in fds:
            try:
                self._analyze_file(ca, fd)
            except Exception as e:  # noqa: BLE001  never crash: fall back to all tests
                ca.kinds.add("fallback")
                ca.notes.append(f"{fd.path}: analysis error {e!r}")
                ca.cand |= self.ix.all_tests()
        return ca

    def _analyze_file(self, ca, fd):
        ix = self.ix
        path = fd.path
        if fd.binary:
            ca.verifiable = False
            ca.kinds.add("other")
            ca.cand |= ix.readers(path) | ix.readers(fd.old_path or path)
            ca.cand |= ix.listers(os.path.dirname(path))
            if path.endswith(".py"):
                ca.cand |= ix.file_any(path)
            ca.notes.append(f"{path}: binary")
            return
        old_rel = fd.old_path if fd.old_path and fd.old_path != "/dev/null" else path
        if fd.is_new:
            old = ""
        else:
            try:
                old = _read(self.root, old_rel)
            except OSError:
                # patch modifies a file we do not have: be safe
                ca.kinds.add("fallback")
                ca.verifiable = False
                ca.cand |= ix.all_tests()
                ca.notes.append(f"{path}: missing base file")
                return
        if fd.is_deleted:
            new = None
        else:
            try:
                new = apply_filediff(old, fd)
            except Exception as e:  # noqa: BLE001
                ca.kinds.add("fallback")
                ca.verifiable = False
                ca.cand |= ix.all_tests()
                ca.notes.append(f"{path}: patch does not apply ({e})")
                return
        if fd.rename and old_rel != path:
            ca.files[old_rel] = None
        ca.files[path] = new
        if path.endswith(".py") or old_rel.endswith(".py"):
            self._analyze_py(ca, fd, old_rel, path, old, new)
        else:
            self._analyze_data(ca, fd, old_rel, path, old, new)

    # ---- python ----
    def _analyze_py(self, ca, fd, old_rel, path, old, new):
        ix = self.ix
        in_pkg = path.startswith(("sqlglot/", "tests/")) or old_rel.startswith(("sqlglot/", "tests/"))
        if fd.is_new and not old:
            # a brand-new module only matters if something imports or lists it
            c = ix.listers(os.path.dirname(path))
            ca.kinds.add("py_new")
            ca.cand |= c
            ca.notes.append(f"{path}: new python file, listers {len(c)}")
            return
        if new is None or (fd.rename and old_rel != path):
            c = ix.file_any(old_rel) | ix.listers(os.path.dirname(old_rel)) | ix.readers(old_rel)
            ca.kinds.add("py_top")
            ca.import_time = True
            ca.cand |= c
            ca.notes.append(f"{old_rel}: deleted/renamed, users {len(c)}")
            return
        try:
            tree = self._tree(old_rel)
        except SyntaxError:
            ca.kinds.add("fallback")
            ca.cand |= ix.file_any(path) | ix.readers(path)
            return
        try:
            new_tree = ast.parse(new)
        except (SyntaxError, ValueError):
            new_tree = None
        if new_tree is None:
            # the module no longer compiles: every test that imports it fails
            ca.py_changed.add(path)
            c = ix.file_any(path)
            ca.kinds.add("py_syntax")
            ca.import_time = True
            ca.cand |= c
            ca.notes.append(f"{path}: syntax error, importers {len(c)}")
            return
        semantic = []
        for reg in changed_regions_detailed(fd):
            if not self._region_is_noop(old_rel, tree, new_tree, reg):
                semantic.append((reg["kind"], reg["a"], reg["b"]))
        if not semantic:
            ca.kinds.add("py_noop")
            ca.notes.append(f"{path}: no semantic change (comments/formatting)")
            return
        ca.py_changed.add(path)
        ca.cand |= ix.readers(path)
        for kind, a, b in semantic:
            a2, b2 = (a, a + 1) if kind == "ins" else (a, b)
            loc = locate(tree, a2, b2)
            if loc.stmt is None:
                # outside any statement (e.g. trailing lines): treat as top level of module
                c = ix.file_any(path)
                ca.kinds.add("py_top")
                ca.import_time = True
                ca.cand |= c
                ca.notes.append(f"{path}:{a}-{b} {kind} no stmt -> importers {len(c)}")
                continue
            span = loc.stmt_span or (a2, b2)
            if kind == "ins" and loc.func is not None:
                # insertion: also anything reaching the neighbouring statements of the block
                span = (min(span[0], a), max(span[1], a + 1))
            if loc.func is not None and not (loc.in_header and isinstance(loc.stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))):
                c = ix.fn_tests(path, *span)
                ca.kinds.add("py_fn")
                if path.startswith("sqlglot/") and ix.runs_at_import(path, *span):
                    # the function also runs while modules import (factories, metaclasses): its run-time
                    # callers are certain; import-time effects come from the differential import
                    rt = ix.fnr_tests(path, *span)
                    ca.cand |= rt
                    ca.diff_fallback |= c
                    ca.needs_diff = True
                    ca.import_time = True
                    ca.notes.append(f"{path}:{a}-{b} {kind} fn {loc.qual} span={span} runs at import: runtime {len(rt)} fallback {len(c)}")
                else:
                    ca.cand |= c
                    ca.notes.append(f"{path}:{a}-{b} {kind} fn {loc.qual} span={span} -> {len(c)}")
                ca.fn_spans.append((path, span[0], span[1]))
                continue
            # import-time code (module / class body, def headers)
            ca.import_time = True
            ca.kinds.add("py_top")
            if path.startswith("sqlglot/"):
                importers = ix.file_any(path)
                ca.diff_fallback |= importers
                ca.needs_diff = True
                ca.notes.append(f"{path}:{a}-{b} {kind} import-time {type(loc.stmt).__name__} -> diff (fallback importers {len(importers)})")
                continue
            c = self._top_level_candidates(ca, path, tree, loc, span)
            ca.cand |= c

    def _top_level_candidates(self, ca, path, tree, loc, span):
        ix = self.ix
        if not path.startswith("sqlglot/"):
            # test modules / helpers: every test that imported the module (sound)
            c = ix.file_any(path)
            ca.notes.append(f"{path}:{span} top-level change in non-sqlglot module -> importers {len(c)}")
            return c
        importers = ix.top_tests(path, *span) | ix.fn_tests(path, *span)
        stmt = loc.stmt
        # def header change (decorator / defaults / signature): callers of the function, plus importers
        # if decorators changed (they run at import)
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = (stmt.body[0].lineno, stmt.end_lineno)
            callers = ix.fn_tests(path, *body)
            deco_lines = {d.lineno for d in stmt.decorator_list}
            if any(span[0] <= dl <= span[1] for dl in deco_lines) or True:
                # decorators/defaults are evaluated at import, but only observed through calls
                # (or through registries built by decorators) -> callers + name users
                users = self._name_users([stmt.name], path, span)
                c = (callers | users) & (importers or callers | users)
                ca.notes.append(f"{path}: def header {stmt.name} callers {len(callers)} users {len(users)} -> {len(c)}")
                return c
        names = _bound_names(stmt)
        if names:
            users = self._name_users(names, path, span)
            if users is None:
                ca.notes.append(f"{path}:{span} top names={names}: read at import -> importers {len(importers)}")
                return importers
            c = users & importers
            ca.notes.append(f"{path}:{span} top names={names} importers={len(importers)} users={len(users)} -> {len(c)}")
            return c
        ca.notes.append(f"{path}:{span} top {type(stmt).__name__} -> importers {len(importers)}")
        return importers

    def _name_users(self, names, path, span, depth=0):
        """Tests that execute at run time a line mentioning any of names. None if a name is read by
        code that runs at import time (so derived state may differ) and we cannot narrow."""
        ix = self.ix
        users = set()
        seen = set(names)
        work = list(names)
        while work:
            nm = work.pop()
            if len(nm) <= 2:
                return None
            for (f, ln) in self.name_lines(nm):
                if f == path and span[0] <= ln <= span[1]:
                    continue
                t = ix.fnr_tests(f, ln, ln)
                if t:
                    users |= t
                # read at import time by other top-level code / metaclasses?
                top = ix.top_tests(f, ln, ln)
                if top and f.startswith("sqlglot/"):
                    tgt = self._top_targets(f, ln)
                    if tgt is None:
                        return None
                    for t2 in tgt:
                        if t2 not in seen and depth < 3:
                            seen.add(t2)
                            work.append(t2)
                if f.startswith("sqlglot/") and self._is_import_time_fn_line(f, ln):
                    return None
        return users

    def _string_spans(self, rel, tree):
        key = ("strspans", rel)
        sp = self._src_cache.get(key)
        if sp is None:
            sp = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.Constant, ast.JoinedStr)) and getattr(node, "end_lineno", None) and node.end_lineno > node.lineno:
                    if isinstance(node, ast.JoinedStr) or isinstance(node.value, (str, bytes)):
                        sp.append((node.lineno, node.end_lineno))
            self._src_cache[key] = sp
        return sp

    def _region_is_noop(self, rel, tree, new_tree, reg):
        """True if the region cannot change behaviour (comment/blank-only edits outside strings, or the
        innermost enclosing statement is AST-identical before and after)."""
        lines = reg["rem"] + reg["add"]
        if all((not t.strip()) or t.strip().startswith("#") for t in lines):
            if reg["a"] <= 2 and any(_CODING_RE.match(t) for t in lines):
                return False
            spans = self._string_spans(rel, tree)
            if reg["kind"] == "ins":
                gap = reg["a"]  # text goes between old lines gap and gap+1
                inside = any(s0 <= gap < e0 for s0, e0 in spans)
            else:
                inside = any(s0 < ln <= e0 for s0, e0 in spans for ln in range(reg["a"], reg["b"] + 1))
            return not inside
        # compare the innermost enclosing statements
        if reg["kind"] == "ins":
            oa, ob = reg["a"], reg["a"] + 1
        else:
            oa, ob = reg["a"], reg["b"]
        if reg["add"]:
            na, nb = reg["na"], reg["nb"]
        else:
            na, nb = reg["na"] - 1, reg["na"]
        lo = locate(tree, oa, ob)
        ln = locate(new_tree, max(1, na), max(1, nb))
        so, sn = lo.stmt, ln.stmt
        if so is None or sn is None or type(so) is not type(sn):
            return False
        if (so.end_lineno - so.lineno) > 400:
            return False
        return ast.dump(so, include_attributes=False) == ast.dump(sn, include_attributes=False)

    def runtime_name_users(self, name):
        users = set()
        for (f, ln) in self.name_lines(name):
            users |= self.ix.fnr_tests(f, ln, ln)
        return users

    def _is_import_time_fn_line(self, f, ln):
        fni = getattr(self.ix, "fni", None)
        if fni is None:
            return False
        fm = fni.get(f)
        return bool(fm and ln in fm)

    def _top_targets(self, f, ln):
        """Names bound by the top-level statement at f:ln (for following derived bindings)."""
        try:
            tree = self._tree(f)
        except SyntaxError:
            return None
        loc = locate(tree, ln, ln)
        if loc.stmt is None:
            return None
        if loc.func is not None:
            return []  # it is inside a function after all
        names = _bound_names(loc.stmt)
        if not names and isinstance(loc.stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            return [loc.stmt.name]
        return names

    def _tree(self, f):
        key = ("tree", f)
        t = self._src_cache.get(key)
        if t is None:
            t = ast.parse(self._text(f))
            self._src_cache[key] = t
        return t

    # ---- data / fixtures ----
    def _analyze_data(self, ca, fd, old_rel, path, old, new):
        ix = self.ix
        c = ix.readers(path) | ix.readers(old_rel)
        if fd.is_new or fd.is_deleted or (fd.rename and old_rel != path):
            c |= ix.listers(os.path.dirname(path)) | ix.listers(os.path.dirname(old_rel))
        if c and path.endswith(".sql") and new is not None and old is not None and not fd.is_new:
            if _sql_fixture_equivalent(old, new):
                ca.kinds.add("fixture_noop")
                ca.notes.append(f"{path}: fixture change only in comments/blank lines (helpers ignore them)")
                # Tests that read the raw file could still notice; keep those that are not helper users.
                # All sqlglot fixture readers go through tests/helpers.py, which drops such lines.
                return
        ca.kinds.add("fixture" if path.startswith("tests/fixtures/") else "other")
        ca.cand |= c
        ca.notes.append(f"{path}: readers {len(c)}")


def _bound_names(stmt):
    names = []
    tgts = []
    if isinstance(stmt, ast.Assign):
        tgts = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        tgts = [stmt.target]
    elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [stmt.name]
    for tg in tgts:
        for node in ast.walk(tg):
            if isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
    return names


def _filter_comments(s):
    # mirror of tests/helpers.py::_filter_comments
    return "\n".join([line for line in s.splitlines() if line and not line.startswith("--")])


def _sql_fixture_equivalent(old, new):
    return _filter_comments(old) == _filter_comments(new)
