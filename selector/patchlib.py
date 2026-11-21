"""Patch parsing and static change analysis for Blast Radius (research version)."""
import ast
import re

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FileDiff:
    def __init__(self):
        self.old_path = None
        self.new_path = None
        self.hunks = []  # list of [old_start, old_len, new_start, new_len, lines]
        self.is_new = False
        self.is_deleted = False
        self.binary = False
        self.rename = False

    @property
    def path(self):
        return self.new_path if self.new_path and self.new_path != "/dev/null" else self.old_path

    def __repr__(self):
        return f"FileDiff({self.old_path}->{self.new_path}, {len(self.hunks)} hunks)"


def _strip_prefix(p):
    p = p.strip()
    if "\t" in p:
        p = p.split("\t", 1)[0]
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p == "/dev/null":
        return p
    if p.startswith("a/") or p.startswith("b/"):
        return p[2:]
    return p


def parse_patch(text):
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FileDiff()
            files.append(cur)
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if m:
                cur.old_path, cur.new_path = m.group(1), m.group(2)
            i += 1
            continue
        if cur is None and (line.startswith("--- ") or line.startswith("+++ ")):
            cur = FileDiff()
            files.append(cur)
        if cur is not None:
            if line.startswith("new file mode"):
                cur.is_new = True
            elif line.startswith("deleted file mode"):
                cur.is_deleted = True
            elif line.startswith("rename from ") or line.startswith("rename to "):
                cur.rename = True
                if line.startswith("rename from "):
                    cur.old_path = line[len("rename from "):]
                else:
                    cur.new_path = line[len("rename to "):]
            elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
                cur.binary = True
            elif line.startswith("--- "):
                p = _strip_prefix(line[4:])
                cur.old_path = p if p != "/dev/null" else cur.old_path
                if p == "/dev/null":
                    cur.is_new = True
            elif line.startswith("+++ "):
                p = _strip_prefix(line[4:])
                cur.new_path = p if p != "/dev/null" else cur.new_path
                if p == "/dev/null":
                    cur.is_deleted = True
            elif line.startswith("@@"):
                m = HUNK_RE.match(line)
                if m:
                    os_, ol, ns, nl = m.groups()
                    ol = int(ol) if ol is not None else 1
                    nl = int(nl) if nl is not None else 1
                    h = [int(os_), ol, int(ns), nl, []]
                    cur.hunks.append(h)
                    i += 1
                    # consume hunk body by counts
                    rem_old, rem_new = ol, nl
                    while i < n and (rem_old > 0 or rem_new > 0):
                        l2 = lines[i]
                        if l2.startswith("\\"):
                            h[4].append(l2)
                            i += 1
                            continue
                        tag = l2[:1]
                        if tag == " " or l2 == "":
                            rem_old -= 1
                            rem_new -= 1
                            h[4].append(" " + l2[1:] if l2 else " ")
                        elif tag == "-":
                            rem_old -= 1
                            h[4].append(l2)
                        elif tag == "+":
                            rem_new -= 1
                            h[4].append(l2)
                        else:
                            break
                        i += 1
                    # trailing "\ No newline" markers
                    while i < n and lines[i].startswith("\\"):
                        h[4].append(lines[i])
                        i += 1
                    continue
        i += 1
    return files


def apply_filediff(old_text, fd):
    """Apply hunks to old_text (str). Returns new text. Raises on mismatch."""
    if fd.is_new:
        old_lines = []
    else:
        old_lines = old_text.split("\n")
    out = []
    pos = 0  # 0-based index into old_lines
    no_newline_new = False
    shift = 0  # like git apply: a hunk whose context sits at an offset is relocated (no fuzz)
    for h in fd.hunks:
        os_, ol, ns, nl, body = h
        start = (os_ - 1 if ol > 0 else os_) + shift
        old_side = [l[1:] for l in body if l[:1] in (" ", "-")]
        if old_side and old_lines[start:start + len(old_side)] != old_side:
            best = None
            for cand in range(pos, len(old_lines) - len(old_side) + 1):
                if old_lines[cand:cand + len(old_side)] == old_side:
                    if best is None or abs(cand - start) < abs(best - start):
                        best = cand
                    if cand > start and best is not None:
                        break
            if best is None:
                raise ValueError(f"hunk at old line {os_} does not match")
            shift += best - start
            start = best
            # record the real position so changed-region computations stay in old-file coordinates
            h[0] = start + 1 if ol > 0 else start
        if start < pos:
            raise ValueError("overlapping hunks")
        out.extend(old_lines[pos:start])
        pos = start
        prev_tag = None
        for l in body:
            if l.startswith("\\"):
                if prev_tag == "+":
                    no_newline_new = True
                continue
            tag, content = l[:1], l[1:]
            prev_tag = tag
            if tag == " ":
                if pos >= len(old_lines) or old_lines[pos] != content:
                    raise ValueError(f"context mismatch at old line {pos+1}")
                out.append(content)
                pos += 1
            elif tag == "-":
                if pos >= len(old_lines) or old_lines[pos] != content:
                    raise ValueError(f"delete mismatch at old line {pos+1}")
                pos += 1
            elif tag == "+":
                out.append(content)
    out.extend(old_lines[pos:])
    text = "\n".join(out)
    return text


def changed_regions(fd):
    """Return list of (kind, old_a, old_b) in OLD line numbers (1-based, inclusive).
    kind 'mod' = old lines a..b were removed/replaced; 'ins' = insertion between old line a and a+1
    (b == a)."""
    regs = []
    for (os_, ol, ns, nl, body) in fd.hunks:
        old_ln = os_ if ol > 0 else os_ + 1
        run_del = []
        run_add = False
        for l in body + [" __END__"]:
            if l.startswith("\\"):
                continue
            tag = l[:1]
            if tag == "-":
                run_del.append(old_ln)
                old_ln += 1
            elif tag == "+":
                run_add = True
            else:
                if run_del:
                    regs.append(("mod", run_del[0], run_del[-1]))
                elif run_add:
                    regs.append(("ins", old_ln - 1, old_ln - 1))
                run_del = []
                run_add = False
                old_ln += 1
    return regs


def strip_positions(tree):
    for node in ast.walk(tree):
        for a in ("lineno", "col_offset", "end_lineno", "end_col_offset"):
            if hasattr(node, a):
                try:
                    delattr(node, a)
                except AttributeError:
                    pass
    return tree


def ast_equal(old_src, new_src):
    try:
        a = ast.parse(old_src)
    except SyntaxError:
        return None
    try:
        b = ast.parse(new_src)
    except SyntaxError:
        return False
    return ast.dump(a, include_attributes=False) == ast.dump(b, include_attributes=False)


def changed_regions_detailed(fd):
    """Like changed_regions, with new-file coordinates and the removed/added text of each region.
    Returns list of dicts: kind ('mod'|'ins'), a, b (old lines), na, nb (new lines), rem, add."""
    regs = []
    for (os_, ol, ns, nl, body) in fd.hunks:
        old_ln = os_ if ol > 0 else os_ + 1
        new_ln = ns if nl > 0 else ns + 1
        run_del, run_add = [], []
        add_start = None
        for l in body + [" __END__"]:
            if l.startswith("\\"):
                continue
            tag = l[:1]
            if tag == "-":
                run_del.append((old_ln, l[1:]))
                old_ln += 1
            elif tag == "+":
                if add_start is None:
                    add_start = new_ln
                run_add.append(l[1:])
                new_ln += 1
            else:
                if run_del or run_add:
                    if run_del:
                        a, b = run_del[0][0], run_del[-1][0]
                        kind = "mod"
                    else:
                        a = b = old_ln - 1
                        kind = "ins"
                    na = add_start if add_start is not None else new_ln
                    nb = (add_start + len(run_add) - 1) if run_add else new_ln - 1
                    regs.append({"kind": kind, "a": a, "b": b, "na": na, "nb": nb,
                                 "rem": [t for _, t in run_del], "add": list(run_add)})
                run_del, run_add = [], []
                add_start = None
                old_ln += 1
                new_ln += 1
    return regs
