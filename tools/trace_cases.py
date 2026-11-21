"""Offline per-case coverage for fixture-driven tests.

Many expensive tests loop over SQL fixture cases (TestOptimizer.check_file, the TPC-H/TPC-DS executor
tests). A code change can only alter a case that executes it, so verifying such a test against a patch only
needs the cases whose execution reaches the changed lines. This tool records, for each target test and each
fixture case (fixture file, index), the sqlglot lines executed when the test runs with *only that case*
(the fixture loader is filtered - the same mechanism the selector uses when verifying). Lines executed
with *no* case (setUpClass, code outside the case loop) are recorded as the test's base.

usage: python trace_cases.py <out_dir> <test_id> [<test_id> ...]
"""
import concurrent.futures
import importlib
import json
import os
import sys
import time
import unittest

sys.path.insert(0, "/app")
sys.path.insert(0, "/sqlglot")
os.chdir("/sqlglot")
os.environ.setdefault("SKIP_INTEGRATION", "0")

ROOT = "/sqlglot/sqlglot/"
mon = sys.monitoring
TOOL = mon.COVERAGE_ID
LINES = {}
FIRST = {}


def on_line(code, line):
    f = code.co_filename
    if f.startswith(ROOT):
        s = LINES.get(f)
        if s is None:
            s = LINES[f] = set()
        s.add(line)
    return mon.DISABLE


def on_start(code, off):
    f = code.co_filename
    if f.startswith(ROOT):
        ln = FIRST.get(code)
        if ln is None:
            ln = next((l for _, _, l in code.co_lines() if l is not None), 0)
            FIRST[code] = ln
        s = LINES.get(f)
        if s is None:
            s = LINES[f] = set()
        s.add(ln)
    return mon.DISABLE


def start_tracing():
    LINES.clear()
    try:
        mon.use_tool_id(TOOL, "cases")
    except ValueError:
        pass  # inherited registration from the forking parent
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    mon.register_callback(TOOL, mon.events.PY_START, on_start)
    mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)
    mon.restart_events()


def stop_tracing():
    mon.set_events(TOOL, 0)
    return {f[len("/sqlglot/"):]: sorted(s) for f, s in LINES.items()}


def run_test(mod, tid):
    from selector.engine import _TimedResult

    f, cls, meth = tid.split("::")
    case = getattr(mod, cls)(meth)
    res = _TimedResult({tid: 600.0}, time.monotonic() + 3600)
    res.failfast = False
    case.run(res)  # class fixtures already set up by the caller
    return res.status.get(tid, "unknown")


def main():
    out_dir = sys.argv[1]
    tids = sys.argv[2:]
    os.makedirs(out_dir, exist_ok=True)
    from selector import caseflt, engine

    engine.preload()
    concurrent.futures.ProcessPoolExecutor = engine._InlineExecutor
    import unittest.case as uc

    uc.difflib = engine._NoDiff
    import sqlglot  # noqa: F401
    from selector import fingerprint

    fingerprint.import_all()
    caseflt.install()
    by_class = {}
    for tid in tids:
        f, cls, _ = tid.split("::")
        by_class.setdefault((f, cls), []).append(tid)
    for (f, cls), group in by_class.items():
        mod = importlib.import_module(f[:-3].replace("/", "."))
        caseflt.patch_modules()
        if hasattr(mod, "ProcessPoolExecutor"):
            mod.ProcessPoolExecutor = engine._InlineExecutor
        klass = getattr(mod, cls)
        # class zygote: setUpClass once (its lines are part of every case's base)
        r, w = os.pipe()
        pid = os.fork()
        if pid:
            os.close(w)
            os.waitpid(pid, 0)
            os.close(r)
            continue
        os.close(r)
        try:
            caseflt.FILTER.clear()
            caseflt.COUNTS.clear()
            caseflt.MODE[0] = "count"
            start_tracing()
            klass.setUpClass()
            setup_lines = stop_tracing()
            # loaders called inside setUpClass (executor TPC lists) were counted here
            class_counts = dict(caseflt.COUNTS)
            for tid in group:
                out_path = os.path.join(out_dir, tid.replace("/", "_").replace("::", "__") + ".json")
                if os.path.exists(out_path):
                    continue
                t0 = time.monotonic()
                # 1) count run (no tracing): which fixtures does the test read, how many cases?
                rr, ww = os.pipe()
                cpid = os.fork()
                if cpid == 0:
                    os.close(rr)
                    caseflt.COUNTS.clear()
                    caseflt.MODE[0] = "count"
                    st = run_test(mod, tid)
                    os.write(ww, json.dumps({"st": st, "counts": caseflt.COUNTS}).encode())
                    os._exit(0)
                os.close(ww)
                data = b""
                while True:
                    b = os.read(rr, 65536)
                    if not b:
                        break
                    data += b
                os.close(rr)
                os.waitpid(cpid, 0)
                info = json.loads(data)
                counts = dict(info["counts"])
                for fx, n in class_counts.items():
                    if cls == "TestExecutor":
                        # the TPC lists are loaded for the whole class; each test uses one of them
                        want = "tpc-ds" if "tpcds" in tid else ("tpc-h" if "tpch" in tid else None)
                        if want is None or want not in fx:
                            continue
                    counts.setdefault(fx, n)
                cases = [(fx, i) for fx, n in sorted(counts.items()) for i in range(n)]
                # 2) base run: every loader yields nothing
                # 3) one run per case
                results = {}
                for key in [None] + cases:
                    rr, ww = os.pipe()
                    cpid = os.fork()
                    if cpid == 0:
                        os.close(rr)
                        caseflt.FILTER.clear()
                        caseflt.MODE[0] = "filter"
                        for fx in counts:
                            caseflt.FILTER[fx] = set()
                        if key is not None:
                            caseflt.FILTER[key[0]] = {key[1]}
                        caseflt.apply_class_filters(klass)
                        start_tracing()
                        st = run_test(mod, tid)
                        lines = stop_tracing()
                        os.write(ww, json.dumps({"st": st, "lines": lines}).encode())
                        os._exit(0)
                    os.close(ww)
                    data = b""
                    while True:
                        b = os.read(rr, 1 << 20)
                        if not b:
                            break
                        data += b
                    os.close(rr)
                    os.waitpid(cpid, 0)
                    try:
                        results["base" if key is None else f"{key[0]}#{key[1]}"] = json.loads(data)
                    except ValueError:
                        results["base" if key is None else f"{key[0]}#{key[1]}"] = {"st": "crash", "lines": {}}
                doc = {"id": tid, "full_status": info["st"], "counts": counts, "setup": setup_lines,
                       "runs": results, "seconds": round(time.monotonic() - t0, 2)}
                with open(out_path, "w") as fh:
                    json.dump(doc, fh)
                print(tid, len(cases), "cases", doc["seconds"], "s", file=sys.stderr, flush=True)
        finally:
            os._exit(0)


if __name__ == "__main__":
    main()
