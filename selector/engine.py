"""Local test execution engine: run sqlglot unittests against a patched tree, in-process.

The parent (zygote) imports only stdlib + heavy third-party modules. Each job forks a child that
installs an overlay (patched files served from memory / tmp), imports sqlglot fresh, and runs the
requested tests with ProcessPoolExecutor replaced by an inline executor (with pickle round trips,
so serialization paths still execute). Results come back over a pipe.
"""
import builtins
import concurrent.futures
import importlib.abc
import importlib.machinery
import importlib.util
import io
import json
import os
import pickle
import signal
import sys
import time
import unittest


class _LazyFuture(concurrent.futures.Future):
    """A future that is already 'finished' for wait()/as_completed() but computes on first result().
    Tests that fail stop at their first failing subtest, so later tasks are never computed."""

    def __init__(self, thunk):
        super().__init__()
        self._thunk = thunk
        self._state = concurrent.futures._base.FINISHED

    def _eval(self):
        th = self._thunk
        if th is not None:
            self._thunk = None
            try:
                self._result = th()
            except BaseException as e:  # noqa: BLE001
                self._exception = e

    def result(self, timeout=None):
        self._eval()
        if self._exception is not None:
            raise self._exception
        return self._result

    def exception(self, timeout=None):
        self._eval()
        return self._exception


class _InlineExecutor:
    """Stand-in for ProcessPoolExecutor: runs tasks in-process, lazily, with pickle round trips so the
    serialization paths a real pool exercises still run."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, /, *args, **kwargs):
        blob = pickle.dumps((fn, args, kwargs))

        def thunk():
            fn2, a2, k2 = pickle.loads(blob)
            return pickle.loads(pickle.dumps(fn2(*a2, **k2)))

        return _LazyFuture(thunk)

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return (self.submit(fn, *a).result() for a in zip(*iterables))

    def shutdown(self, wait=True, cancel_futures=False):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _NoDiff:
    """unittest only needs to know that a comparison failed; skip building (quadratic) diffs."""

    @staticmethod
    def ndiff(a, b, *args, **kw):
        return iter(["(diff omitted)"])

    @staticmethod
    def unified_diff(*a, **k):
        return iter(["(diff omitted)"])


class _OverlayFinder(importlib.abc.MetaPathFinder):
    def __init__(self, root, py_overrides, deleted):
        self.root = root
        self.py = py_overrides  # abs path -> source text
        self.deleted = deleted  # set of abs paths

    def find_spec(self, fullname, path=None, target=None):
        parts = fullname.split(".")
        if parts[0] not in ("sqlglot", "tests"):
            return None
        base = os.path.join(self.root, *parts)
        cands = [(base + ".py", False), (os.path.join(base, "__init__.py"), True)]
        for p, is_pkg in cands:
            if p in self.deleted:
                # fall through to the other candidate; if none, block import
                continue
            if p in self.py:
                loader = _OverlayLoader(p, self.py[p])
                spec = importlib.util.spec_from_file_location(
                    fullname, p, loader=loader,
                    submodule_search_locations=[os.path.dirname(p)] if is_pkg else None)
                return spec
        if any(p in self.deleted for p, _ in cands):
            if not any(os.path.exists(p) and p not in self.deleted for p, _ in cands):
                raise ModuleNotFoundError(f"No module named {fullname!r} (deleted by patch)")
        return None


class _OverlayLoader(importlib.abc.SourceLoader):
    def __init__(self, path, src):
        self.path = path
        self.src = src

    def get_filename(self, fullname):
        return self.path

    def get_data(self, path):
        if path == self.path:
            return self.src.encode("utf-8")
        with open(path, "rb") as f:
            return f.read()

    def path_stats(self, path):
        raise OSError  # never use bytecode cache for overlay files

    def set_data(self, path, data, *, _mode=0o666):
        return None


def _install_overlay(root, files, tmpdir):
    """files: relpath -> new text (str) or None (deleted)."""
    py, deleted, redirect = {}, set(), {}
    for rp, txt in files.items():
        ap = os.path.join(root, rp)
        if txt is None:
            deleted.add(ap)
            continue
        if rp.endswith(".py"):
            py[ap] = txt
        # every patched file also gets a real copy (for open() and for .py read as data)
        dst = os.path.join(tmpdir, rp)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8", newline="") as fh:
            fh.write(txt)
        redirect[ap] = dst
    sys.meta_path.insert(0, _OverlayFinder(root, py, deleted))
    real_open = builtins.open

    def _map(p):
        try:
            s = os.fspath(p)
        except TypeError:
            return p
        if isinstance(s, bytes):
            return p
        a = s if os.path.isabs(s) else os.path.abspath(s)
        a = os.path.normpath(a)
        if a in deleted:
            raise FileNotFoundError(2, "No such file or directory (deleted by patch)", s)
        return redirect.get(a, p)

    def patched_open(file, *args, **kwargs):
        if isinstance(file, int):
            return real_open(file, *args, **kwargs)
        return real_open(_map(file), *args, **kwargs)

    builtins.open = patched_open
    io.open = patched_open
    real_open_code = io.open_code

    def patched_open_code(path):
        # runpy.run_path and friends read sources through io.open_code
        return real_open_code(_map(path))

    io.open_code = patched_open_code
    real_exists = os.path.exists
    real_isfile = os.path.isfile

    def exists(p):
        try:
            a = os.path.normpath(os.path.abspath(os.fspath(p)))
        except TypeError:
            return real_exists(p)
        if a in deleted:
            return False
        if a in redirect:
            return True
        return real_exists(p)

    def isfile(p):
        try:
            a = os.path.normpath(os.path.abspath(os.fspath(p)))
        except TypeError:
            return real_isfile(p)
        if a in deleted:
            return False
        if a in redirect:
            return True
        return real_isfile(p)

    os.path.exists = exists
    os.path.isfile = isfile


class _Timeout(BaseException):
    pass


def _alarm(signum, frame):
    raise _Timeout()


_ALWAYS = set()
TIMES = {}
_STATE = {"failed": False, "stop_after_fail": False}


PYTEST_DEPTH = 40  # stack depth of a test body under pytest-xdist (37 measured) plus a margin


def _emulate_pytest_stack():
    """Labels come from pytest, whose machinery puts each test ~30 frames deeper than ours. A change that
    pushes recursion near the limit raises RecursionError there first: lower our limit by the deficit."""
    f = sys._getframe()
    d = 0
    while f is not None:
        d += 1
        f = f.f_back
    deficit = PYTEST_DEPTH - (d + 5)  # unittest adds ~5 frames between here and the test method
    if deficit > 0:
        sys.setrecursionlimit(max(100, sys.getrecursionlimit() - deficit))


def _run_tests(test_ids, per_test_timeout, budget_deadline):
    """Run tests grouped by class. Returns {test_id: 'pass'|'fail'|'timeout'|'skip'|'unknown'}."""
    _emulate_pytest_stack()
    concurrent.futures.ProcessPoolExecutor = _InlineExecutor
    import unittest.case as _uc

    _uc.difflib = _NoDiff
    out = {}
    groups = {}
    order = []
    for tid in test_ids:
        f, cls, meth = tid.split("::")
        key = (f, cls)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((meth, tid))
    signal.signal(signal.SIGALRM, _alarm)
    for (f, cls) in order:
        meths = groups[(f, cls)]
        if time.monotonic() > budget_deadline:
            for _, tid in meths:
                out[tid] = "unknown"
            continue
        modname = f[:-3].replace("/", ".")
        try:
            if _STATE.get("case_filters"):
                from . import caseflt

                if caseflt.MODE[0] != "filter":
                    caseflt.install()
                    caseflt.FILTER.clear()
                    caseflt.FILTER.update({fx: set(v) for fx, v in _STATE["case_filters"].items()})
                    caseflt.MODE[0] = "filter"
            mod = importlib.import_module(modname)
            if _STATE.get("case_filters"):
                caseflt.patch_modules()
            if hasattr(mod, "ProcessPoolExecutor"):
                mod.ProcessPoolExecutor = _InlineExecutor
            klass = getattr(mod, cls)
        except BaseException as e:  # noqa: BLE001  import failure => every test in file fails
            for _, tid in meths:
                out[tid] = "fail"
            continue
        suite = unittest.TestSuite()
        cases = {}
        for meth, tid in meths:
            try:
                case = klass(meth)
            except BaseException:  # noqa: BLE001
                out[tid] = "fail"
                continue
            cases[id(case)] = tid
            suite.addTest(case)
        result = _TimedResult(per_test_timeout, budget_deadline)
        try:
            suite.run(result)
        except _Timeout:
            pass
        for tid in cases.values():
            out[tid] = result.status.get(tid, "unknown")
        # class-level failure (setUpClass error) marks all as fail
        for tid in cases.values():
            if out[tid] == "unknown" and result.class_error:
                out[tid] = "fail"
    signal.setitimer(signal.ITIMER_REAL, 0)
    return out


class _TimedResult(unittest.TestResult):
    def __init__(self, per_test_timeout, deadline):
        super().__init__()
        self.status = {}
        self.per = per_test_timeout
        self.deadline = deadline
        self.class_error = False
        # a failing subtest already decides the test: unittest then raises _ShouldStop inside the test
        self.failfast = True
        self._ours = set()

    def stop(self):
        return None  # failfast must stop the current test only, never the suite

    def _tid(self, test):
        tc = getattr(test, "test_case", test)  # _SubTest -> parent
        mod = type(tc).__module__.replace(".", "/") + ".py"
        return f"{mod}::{type(tc).__name__}::{tc._testMethodName}"

    def startTest(self, test):
        super().startTest(test)
        tid = self._tid(test)
        self._t0 = time.process_time()
        if _STATE["failed"] and _STATE["stop_after_fail"] and tid not in _ALWAYS:
            # the change already breaks something: the rest is cheaper to include than to run
            self.status[tid] = "unknown"
            self._ours.add(tid)
            raise unittest.SkipTest("stopped after first failure")
        self.status[tid] = "pass"
        lim = self.per.get(tid, 30.0) if isinstance(self.per, dict) else self.per
        lim = min(lim, max(0.05, self.deadline - time.monotonic()))
        signal.setitimer(signal.ITIMER_REAL, lim)

    def stopTest(self, test):
        signal.setitimer(signal.ITIMER_REAL, 0)
        TIMES[self._tid(test)] = round(time.process_time() - getattr(self, "_t0", time.process_time()), 4)
        super().stopTest(test)

    def _mark_fail(self, test, err):
        if isinstance(test, unittest.suite._ErrorHolder):  # setUpClass / module fixture error
            self.class_error = True
            return
        tid = self._tid(test)
        if err is not None and err[0] is _Timeout:
            self.status[tid] = "timeout"
        else:
            self.status[tid] = "fail"
        _STATE["failed"] = True

    # no traceback formatting: only the verdict matters
    def addError(self, test, err):
        self._mark_fail(test, err)

    def addFailure(self, test, err):
        self._mark_fail(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._mark_fail(subtest, err)

    def addUnexpectedSuccess(self, test):
        self.status[self._tid(test)] = "fail"

    def addSkip(self, test, reason):
        tid = self._tid(test)
        self.status[tid] = "unknown" if tid in self._ours else "skip"

    def addExpectedFailure(self, test, err):
        return None


def _child_main(root, files, test_ids, per_test_timeout, budget, tmpdir, w, opts=None):
    try:
        # the selector's own heap (indexes, ASTs) is inherited by fork: freeze it so the cyclic GC
        # (which several tests trigger explicitly) only walks objects created by the tests
        import gc

        gc.freeze()
        if opts:
            _ALWAYS.clear()
            _ALWAYS.update(opts.get("always", ()))
            _STATE["stop_after_fail"] = bool(opts.get("stop_after_fail"))
            _STATE["case_filters"] = opts.get("case_filters") or {}
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.environ.setdefault("SKIP_INTEGRATION", "0")
        os.makedirs(tmpdir, exist_ok=True)
        _install_overlay(root, files, os.path.join(tmpdir, str(os.getpid())))
        if root not in sys.path:
            sys.path.insert(0, root)
        os.chdir(root)
        deadline = time.monotonic() + budget
        try:
            t_imp = time.process_time()
            import sqlglot  # noqa: F401
            TIMES["__import__"] = round(time.process_time() - t_imp, 4)
            res = _run_tests(test_ids, per_test_timeout, deadline)
            if os.environ.get("BR_VERBOSE"):
                res["__times__"] = TIMES
        except BaseException:  # noqa: BLE001  sqlglot itself fails to import -> everything fails
            res = {tid: "fail" for tid in test_ids}
        data = json.dumps(res).encode()
        view = memoryview(data)
        while view:
            n = os.write(w, view)
            view = view[n:]
    finally:
        os._exit(0)


def _start(root, files, test_ids, per_test_timeout, budget, tmpdir, opts=None):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        _child_main(root, files, test_ids, per_test_timeout, budget, tmpdir, w, opts)
    os.close(w)
    return pid, r


def run_job(root, files, test_ids, per_test_timeout=30.0, budget=120.0, tmpdir="/tmp/brov"):
    """Fork a child that runs test_ids against root+files overlay. Returns (results, cpu_seconds, wall)."""
    out = {}

    class _J:
        pass

    j = _J()
    j.files, j.ids, j.per, j.budget = files, test_ids, per_test_timeout, budget
    stats = run_many(root, [(j, test_ids, per_test_timeout)], time.monotonic() + budget + 10, parallel=1,
                     tmpdir=tmpdir, budget_of=lambda job: budget)
    return j.verified, stats[0][0], stats[0][1]


def _cleanup(path):
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def run_many(root, jobs, deadline, parallel=2, log=None, tmpdir="/tmp/brov", budget_of=None):
    """jobs: list of (plan, test_ids, per_test_timeout). plan.ca.files (or plan.files) is the overlay.
    Sets plan.verified = {test_id: status}. Returns list of (cpu, wall) per job."""
    import select
    import unittest  # noqa: F401  (every child needs these: import once before forking)
    import unittest.mock  # noqa: F401
    pending = list(range(len(jobs)))
    running = {}  # fd -> (job_index, pid, t_start, chunks, job_deadline)
    stats = [(0.0, 0.0)] * len(jobs)
    while pending or running:
        now = time.monotonic()
        while pending and len(running) < parallel and now < deadline - 1.0:
            k = pending.pop(0)
            plan, ids, per = jobs[k][:3]
            opts = jobs[k][3] if len(jobs[k]) > 3 else None
            files = plan.ca.files if hasattr(plan, "ca") else plan.files
            b = budget_of(plan) if budget_of else min(deadline - now - 0.5, 4.0 * getattr(plan, "est", 5.0) + 5.0)
            b = max(0.5, b)
            pid, r = _start(root, files, ids, per, b, tmpdir, opts)
            running[r] = (k, pid, now, [], now + b + 2.0)
        if not running:
            for k in pending:
                jobs[k][0].verified = {tid: "unknown" for tid in jobs[k][1]}
            pending = []
            break
        timeout = max(0.0, min(v[4] for v in running.values()) - time.monotonic())
        rl, _, _ = select.select(list(running), [], [], timeout)
        now = time.monotonic()
        done = []
        for r in rl:
            b = os.read(r, 1 << 16)
            if b:
                running[r][3].append(b)
            else:
                done.append((r, False))
        for r, v in running.items():
            if v[4] <= now and (r, False) not in done:
                try:
                    os.kill(v[1], signal.SIGKILL)
                except ProcessLookupError:
                    pass
                done.append((r, True))
        for r, killed in done:
            k, pid, ts, chunks, _ = running.pop(r)
            os.close(r)
            _, status, ru = os.wait4(pid, 0)
            _cleanup(os.path.join(tmpdir, str(pid)))  # /tmp is small (64 MB): drop the job's overlay copies
            plan, ids, per = jobs[k][:3]
            res = {}
            if not killed:
                try:
                    res = json.loads(b"".join(chunks).decode())
                except ValueError:
                    res = {}
            times = res.pop("__times__", None)
            for tid in ids:
                res.setdefault(tid, "unknown")
            plan.verified = res
            if times and log:
                top = sorted(times.items(), key=lambda kv: -kv[1])[:6]
                log(f"    times {getattr(plan, 'cid', k)}: " + ", ".join(f"{t.rsplit('::', 1)[-1]}={v}" for t, v in top))
            stats[k] = (ru.ru_utime + ru.ru_stime, time.monotonic() - ts)
            if log:
                nf = sum(1 for s in res.values() if s != "pass")
                log(f"  verified {getattr(plan, 'cid', k)}: {len(ids)} tests, {nf} not passing, cpu {stats[k][0]:.2f}s wall {stats[k][1]:.2f}s{' KILLED' if killed else ''}")
    return stats


def preload():
    import duckdb  # noqa: F401
    import numpy  # noqa: F401
    import pandas  # noqa: F401
    import pandas.testing  # noqa: F401
    import sqlite3  # noqa: F401
    import decimal  # noqa: F401
    import unittest.mock  # noqa: F401
