"""Record function-body lines that run while importing all of sqlglot (metaclasses, factories called in
class bodies, __init_subclass__...). Output JSON {file: [lines]} (paths relative to /sqlglot)."""
import json, os, sys
sys.path.insert(0, "/app"); sys.path.insert(0, "/sqlglot"); os.chdir("/sqlglot")
mon = sys.monitoring
ROOT = "/sqlglot/"
fn = {}
first = {}
def on_line(code, line):
    if code.co_flags & 1 and code.co_filename.startswith(ROOT):
        fn.setdefault(code.co_filename[len(ROOT):], set()).add(line)
    return mon.DISABLE
def on_start(code, off):
    if code.co_filename.startswith(ROOT):
        ln = first.get(code)
        if ln is None:
            ln = next((l for _, _, l in code.co_lines() if l is not None), 0); first[code] = ln
        fn.setdefault(code.co_filename[len(ROOT):], set()).add(ln)
    return mon.DISABLE
import unittest, unittest.mock  # noqa
mon.use_tool_id(1, "imp"); mon.register_callback(1, mon.events.LINE, on_line); mon.register_callback(1, mon.events.PY_START, on_start)
mon.set_events(1, mon.events.LINE | mon.events.PY_START)
from selector import fingerprint as F
F.import_all()
mon.set_events(1, 0)
json.dump({f: sorted(s) for f, s in fn.items()}, open(sys.argv[1], "w"))
print("files", len(fn), "lines", sum(len(s) for s in fn.values()))
