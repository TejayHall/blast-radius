"""Locate changed lines inside a Python module's AST."""
import ast

FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try,
            ast.TryStar, ast.Match, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _header_end(node):
    """Last line of a compound statement's header (before its body)."""
    if isinstance(node, (ast.If, ast.While)):
        return node.test.end_lineno
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return node.iter.end_lineno
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return max(i.context_expr.end_lineno for i in node.items)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if node.body:
            return node.body[0].lineno - 1
        return node.lineno
    if isinstance(node, ast.ClassDef):
        return node.body[0].lineno - 1 if node.body else node.lineno
    if isinstance(node, ast.Match):
        return node.subject.end_lineno
    return node.lineno


class Located:
    def __init__(self):
        self.stmt = None          # innermost statement node containing region
        self.stmt_span = None     # (a, b) lines to use for coverage lookup
        self.func = None          # innermost function/lambda containing region (None => import time)
        self.func_span = None
        self.class_chain = []     # enclosing classes (outer->inner)
        self.in_header = False    # change in header of a compound statement
        self.qual = ""

    def __repr__(self):
        return (f"Located(stmt={type(self.stmt).__name__ if self.stmt else None} span={self.stmt_span} "
                f"func={self.qual!r} func_span={self.func_span} import_time={self.func is None} "
                f"header={self.in_header})")


def locate(tree, a, b):
    """Find innermost statement / function containing old lines a..b."""
    loc = Located()
    quals = []

    def visit_body(stmts, func, func_span):
        for s in stmts:
            s_start = s.lineno
            if getattr(s, "decorator_list", None):
                s_start = min([d.lineno for d in s.decorator_list] + [s.lineno])
            s_end = s.end_lineno
            if s_end < a or s_start > b:
                continue
            # this statement overlaps the region
            loc.stmt = s
            loc.func = func
            loc.func_span = func_span
            loc.stmt_span = (s_start, s_end)
            loc.in_header = False
            if isinstance(s, COMPOUND):
                hdr_end = _header_end(s)
                if b <= hdr_end or a <= hdr_end:
                    # change touches the header (decorators, signature, test expr ...)
                    loc.in_header = True
                    loc.stmt_span = (s_start, hdr_end)
                    if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        # header of a def/class executes at definition time (in the parent scope)
                        _maybe_lambda(s, a, b)
                        return
                    _maybe_lambda_in(s, a, b, hdr_end)
                    return
                # descend into bodies
                if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    quals.append(s.name)
                    loc.qual = ".".join(quals)
                    visit_body(s.body, s, (s.body[0].lineno, s.end_lineno))
                    return
                if isinstance(s, ast.ClassDef):
                    quals.append(s.name)
                    loc.class_chain.append(s)
                    loc.qual = ".".join(quals)
                    visit_body(s.body, func, func_span)
                    return
                for field in ("body", "orelse", "finalbody", "handlers", "cases"):
                    sub = getattr(s, field, None)
                    if not sub:
                        continue
                    if field == "handlers":
                        for h in sub:
                            if h.lineno <= b and h.end_lineno >= a:
                                if h.type is not None and h.type.end_lineno >= a and h.lineno <= b and (not h.body or b < h.body[0].lineno):
                                    loc.stmt_span = (h.lineno, h.body[0].lineno - 1 if h.body else h.lineno)
                                    loc.in_header = True
                                    return
                                visit_body(h.body, func, func_span)
                                return
                        continue
                    if field == "cases":
                        for c in sub:
                            if c.pattern.lineno <= b and c.body[-1].end_lineno >= a:
                                visit_body(c.body, func, func_span)
                                return
                        continue
                    first = sub[0].lineno
                    last = sub[-1].end_lineno
                    if first <= b and last >= a:
                        visit_body(sub, func, func_span)
                        return
                return
            # simple statement: check for lambda inside
            _maybe_lambda(s, a, b)
            return

    def _maybe_lambda_in(s, a, b, hdr_end):
        for node in ast.walk(s):
            if isinstance(node, ast.Lambda) and node.body.lineno <= a and node.body.end_lineno >= b and node.lineno <= hdr_end:
                loc.func = node
                loc.func_span = (node.body.lineno, node.body.end_lineno)
                loc.stmt_span = (node.body.lineno, node.body.end_lineno)
                loc.qual = (loc.qual + ".<lambda>") if loc.qual else "<lambda>"

    def _maybe_lambda(s, a, b):
        best = None
        for node in ast.walk(s):
            if isinstance(node, ast.Lambda):
                bl, be = node.body.lineno, node.body.end_lineno
                if bl <= a and be >= b:
                    if best is None or (be - bl) < (best.body.end_lineno - best.body.lineno):
                        best = node
        if best is not None:
            loc.func = best
            loc.func_span = (best.body.lineno, best.body.end_lineno)
            loc.stmt_span = (best.body.lineno, best.body.end_lineno)
            loc.qual = (loc.qual + ".<lambda>") if loc.qual else "<lambda>"

    visit_body(tree.body, None, None)
    return loc
