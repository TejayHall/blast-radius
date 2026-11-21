p = 'selector/engine.py'
s = open(p, encoding='utf-8').read()
old_exec_start = s.index('class _InlineExecutor:')
old_exec_end = s.index('class _OverlayFinder(')
s = s[:old_exec_start] + '''class _LazyFuture(concurrent.futures.Future):
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


''' + s[old_exec_end:]
# result class: failfast within a test, never stop the suite, no traceback formatting
s = s.replace('''class _TimedResult(unittest.TestResult):
    def __init__(self, per_test_timeout, deadline):
        super().__init__()
        self.status = {}
        self.per = per_test_timeout
        self.deadline = deadline
        self.class_error = False''', '''class _TimedResult(unittest.TestResult):
    def __init__(self, per_test_timeout, deadline):
        super().__init__()
        self.status = {}
        self.per = per_test_timeout
        self.deadline = deadline
        self.class_error = False
        # a failing subtest already decides the test: unittest then raises _ShouldStop inside the test
        self.failfast = True

    def stop(self):
        return None  # failfast must stop the current test only, never the suite''')
s = s.replace('''    def addError(self, test, err):
        super().addError(test, err)
        self._mark_fail(test, err)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._mark_fail(test, err)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._mark_fail(subtest, err)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.status[self._tid(test)] = "fail"

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.status[self._tid(test)] = "skip"''', '''    # no traceback formatting: only the verdict matters
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
        self.status[self._tid(test)] = "skip"

    def addExpectedFailure(self, test, err):
        return None''')
s = s.replace('''    concurrent.futures.ProcessPoolExecutor = _InlineExecutor
    out = {}''', '''    concurrent.futures.ProcessPoolExecutor = _InlineExecutor
    import unittest.case as _uc

    _uc.difflib = _NoDiff
    out = {}''')
open(p, 'w', encoding='utf-8').write(s)
print('ok')
