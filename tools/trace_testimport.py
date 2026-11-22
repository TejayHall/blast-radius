"""Record the sqlglot lines that test-module import runs once all of sqlglot is already imported: code that
test files execute at collection time (module/class level subclasses of sqlglot classes, calls in class
bodies and decorators). The differential import only re-imports sqlglot, so edits reaching these lines
change state it cannot see. Output JSON {test_file: {sqlglot_file: [lines]}} (paths relative to /sqlglot).
usage: python tools/trace_testimport.py <out.json>"""
import json
import os
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, "/sqlglot")
os.chdir("/sqlglot")
ROOT = "/sqlglot/"
os.environ.setdefault("SKIP_INTEGRATION", "0")

import unittest  # noqa: E402,F401
import unittest.mock  # noqa: E402,F401

from selector import fingerprint as F  # noqa: E402

F.import_all()


def trace_module(modname):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            mon = sys.monitoring
            lines = {}
            first = {}

            def on_line(code, line):
                f = code.co_filename
                if f.startswith(ROOT) and not f.startswith(ROOT + "tests/"):
                    lines.setdefault(f[len(ROOT):], set()).add(line)
                return mon.DISABLE

            def on_start(code, off):
                f = code.co_filename
                if f.startswith(ROOT) and not f.startswith(ROOT + "tests/"):
                    ln = first.get(code)
                    if ln is None:
                        ln = next((l for _, _, l in code.co_lines() if l is not None), 0)
                        first[code] = ln
                    lines.setdefault(f[len(ROOT):], set()).add(ln)
                return mon.DISABLE

            mon.use_tool_id(1, "timp")
            mon.register_callback(1, mon.events.LINE, on_line)
            mon.register_callback(1, mon.events.PY_START, on_start)
            mon.set_events(1, mon.events.LINE | mon.events.PY_START)
            err = None
            try:
                __import__(modname)
            except BaseException as e:  # noqa: BLE001
                err = repr(e)
            mon.set_events(1, 0)
            data = json.dumps({"lines": {f: sorted(s) for f, s in lines.items()}, "err": err}).encode()
            view = memoryview(data)
            while view:
                n = os.write(w, view)
                view = view[n:]
        finally:
            os._exit(0)
    os.close(w)
    chunks = []
    while True:
        b = os.read(r, 1 << 16)
        if not b:
            break
        chunks.append(b)
    os.close(r)
    os.waitpid(pid, 0)
    return json.loads(b"".join(chunks) or b"{}")


def main():
    out = {}
    for dirpath, _dirs, files in os.walk(ROOT + "tests"):
        if "fixtures" in dirpath.split(os.sep):
            continue
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), ROOT).replace(os.sep, "/")
            mod = rel[:-3].replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[: -len(".__init__")]
            res = trace_module(mod)
            if res.get("err"):
                print("import error", rel, res["err"][:200], file=sys.stderr)
            if res.get("lines"):
                out[rel] = res["lines"]
    with open(sys.argv[1], "w") as fh:
        json.dump(out, fh, sort_keys=True)
    print("test files with sqlglot lines at import:", len(out), file=sys.stderr)
    for rel, d in sorted(out.items()):
        print(f"  {rel}: " + ", ".join(f"{f}({len(ls)})" for f, ls in sorted(d.items())), file=sys.stderr)


if __name__ == "__main__":
    main()
