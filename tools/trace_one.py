"""Run ONE sqlglot unittest in a fresh interpreter and record exactly what it touched.

Usage: python trace_one.py <out_dir> <test_id> [<test_id> ...]
Each test id is pytest style: tests/test_parser.py::TestParser::test_union.
Every id is run in its own forked child, so caches, lazy imports and module
level code are attributed to the test that needs them (sound per-test coverage).

Output per test: <out_dir>/<n>.json with
  lines_import: {file: [line...]}  lines run before the test started (imports)
  lines_test:   {file: [line...]}  lines run by setUpClass/setUp/test/tearDown
  opens:        [path...]          files opened (audit hook), import + test
  outcome, seconds
"""

import json
import os
import sys
import time

ROOT = "/sqlglot/"


def run_one(test_id, out_path):
    pid = os.fork()
    if pid:
        _, status = os.waitpid(pid, 0)
        return status
    # ---- child ----
    try:
        mon = sys.monitoring
        tool = mon.COVERAGE_ID
        phases = {"import": {}, "test": {}, "import_top": {}, "test_top": {}}
        cur = [phases["import"], phases["import_top"]]

        def on_line(code, line):
            f = code.co_filename
            if f.startswith(ROOT):
                d = cur[0] if code.co_flags & 1 else cur[1]
                s = d.get(f)
                if s is None:
                    s = d[f] = set()
                s.add(line)
            return mon.DISABLE

        first_line = {}

        def on_start(code, offset):
            # LINE events are not emitted for a function's first line when it shares the line with its
            # definition (one-line lambdas in TRANSFORMS/PARSERS dicts): record entry explicitly.
            f = code.co_filename
            if f.startswith(ROOT):
                ln = first_line.get(code)
                if ln is None:
                    ln = 0
                    for _s, _e, l in code.co_lines():
                        if l is not None:
                            ln = l
                            break
                    first_line[code] = ln
                d = cur[0]
                s = d.get(f)
                if s is None:
                    s = d[f] = set()
                s.add(ln)
            return mon.DISABLE

        mon.use_tool_id(tool, "cov")
        mon.register_callback(tool, mon.events.LINE, on_line)
        mon.register_callback(tool, mon.events.PY_START, on_start)
        mon.set_events(tool, mon.events.LINE | mon.events.PY_START)

        opens = set()
        listed = set()

        def audit(ev, args):
            if ev in ("os.listdir", "os.scandir") and args:
                p = args[0]
                if isinstance(p, (str, bytes)):
                    listed.add(os.fsdecode(p))
            if ev == "open" and args:
                p = args[0]
                if isinstance(p, (str, bytes)):
                    p = os.fsdecode(p)
                    if p.startswith(ROOT) or not p.startswith("/"):
                        opens.add(p)

        sys.addaudithook(audit)

        import importlib
        import unittest

        file, cls, meth = test_id.split("::")
        modname = file[:-3].replace("/", ".")
        if "/sqlglot" not in sys.path:
            sys.path.insert(0, "/sqlglot")
        os.chdir("/sqlglot")
        t0 = time.perf_counter()
        err = None
        try:
            mod = importlib.import_module(modname)
            case = getattr(mod, cls)(meth)
        except Exception as e:  # import failure
            err = repr(e)
        t1 = time.perf_counter()
        cur[0] = phases["test"]
        cur[1] = phases["test_top"]
        wdir = out_path + ".workers"
        os.environ["BR_TRACE_DIR"] = wdir
        mon.restart_events()
        outcome = "import_error"
        if err is None:
            result = unittest.TestResult()
            suite = unittest.TestSuite([case])
            suite.run(result)
            if result.failures or result.errors or result.unexpectedSuccesses:
                outcome = "failed"
            elif result.skipped:
                outcome = "skipped"
            else:
                outcome = "passed"
            if result.failures or result.errors:
                err = "\n".join(tb for _, tb in (result.failures + result.errors))[:4000]
        t2 = time.perf_counter()
        mon.set_events(tool, 0)
        os.environ.pop("BR_TRACE_DIR", None)
        workers = {}
        workers_top = {}
        if os.path.isdir(wdir):
            for wf in os.listdir(wdir):
                try:
                    wd = json.load(open(os.path.join(wdir, wf)))
                except Exception:
                    continue
                for f, ls in wd["fn"].items():
                    workers.setdefault(f, set()).update(ls)
                for f, ls in wd["top"].items():
                    workers_top.setdefault(f, set()).update(ls)
        rec = {
            "id": test_id,
            "outcome": outcome,
            "import_seconds": round(t1 - t0, 4),
            "seconds": round(t2 - t1, 4),
            "err": err,
            "lines_import": {f: sorted(s) for f, s in phases["import"].items()},
            "lines_test": {f: sorted(s) for f, s in phases["test"].items()},
            "top_import": {f: sorted(s) for f, s in phases["import_top"].items()},
            "top_test": {f: sorted(s) for f, s in phases["test_top"].items()},
            "listed": sorted(listed),
            "opens": sorted(opens),
            "lines_workers": {f: sorted(s) for f, s in workers.items()},
            "top_workers": {f: sorted(s) for f, s in workers_top.items()},
            "n_workers": len(os.listdir(wdir)) if os.path.isdir(wdir) else 0,
        }
        with open(out_path, "w") as fh:
            json.dump(rec, fh)
    except BaseException as e:  # noqa
        try:
            with open(out_path + ".crash", "w") as fh:
                fh.write(repr(e))
        except Exception:
            pass
    finally:
        os._exit(0)


def main():
    cpus = os.environ.get("BR_CPUS")
    if cpus:
        os.sched_setaffinity(0, {int(c) for c in cpus.split(",")})
    out_dir = sys.argv[1]
    ids = sys.argv[2:]
    if len(ids) == 1 and ids[0].startswith("@"):
        ids = [x.strip() for x in open(ids[0][1:]) if x.strip()]
    os.makedirs(out_dir, exist_ok=True)
    for spec in ids:
        n, tid = spec.split("=", 1)
        out = os.path.join(out_dir, f"{n}.json")
        if os.path.exists(out):
            continue
        run_one(tid, out)


if __name__ == "__main__":
    main()
