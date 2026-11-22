"""Blast Radius selector: for each patch, print the tests to run and how to split them.

Pipeline per commit c:
  1. Static analysis -> C_c, a sound superset of the failing tests F_c (analysis.py).
  2. Import-time changes -> differential import narrows C_c to tests that can observe the changed state.
  3. Verification: tests of C_c that are cheap to run locally (V) are run against the patched tree, so
     only the ones that really fail are kept. Tests that are expensive to verify (X) are kept, except the
     two giant end-to-end tests, which are dropped when the local evidence says the change is inert
     (enough cheap covering tests all pass, nothing timed out or slowed down, no name affinity).
  4. Output the minimum-cost schedule of the final selection (pack.py).
Logs go to stderr; stdout carries exactly one JSON line per commit.
"""
import json
import os
import sys
import time

from .analysis import Analyzer
from .index import Index
from .pack import best_schedule, fill_slack

DATA = os.environ.get("BR_DATA", "/data")
ROOT = os.environ.get("BR_SQLGLOT", "/sqlglot")
W = 2000

HEAVY_FILES = {"tests/test_optimizer.py", "tests/test_executor.py"}
# Tests that check `import sqlglot` in a fresh interpreter: local runs cannot see the patch (the
# subprocess imports the unpatched tree) and the differential import cannot see "eagerly imported"
# changes. Never verify them; add them for any import-time change to a module they load.
SUBPROCESS_TESTS = {"tests/dialects/test_dialect.py::TestDialect::test_lazy_load"}
JOB_OVERHEAD = 0.12      # s CPU: fork + fresh sqlglot import + overlay compile (child)
DIFF_COST = 1.0          # s CPU: one differential import (typical)
HEAVY_PRELOAD = 0.70     # s CPU once: duckdb/pandas/numpy in the zygote
GIANT_MS = 9000          # tests this long are never verified; they may be dropped on strong evidence
CHEAP_MS = 900
MIN_CHEAP_EVIDENCE = 5
PROBE_TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
PROBE_FIXTURE = "optimizer/tpc-ds/tpc-ds.sql"
PROBE_FIXTURE_READER = "tests/test_optimizer.py::TestOptimizer::test_tpcds"
PROBE_K = 4
PROBE_DEFAULT = (2, 27, 52, 77)
EXECUTOR_PREFIX = "tests/test_executor.py::TestExecutor::"
# TPC fixtures are loaded by TestExecutor.setUpClass for every test of the class; only these use them
CLASS_LOADED_FIXTURES = {
    "optimizer/tpc-h/tpc-h.sql": {EXECUTOR_PREFIX + "test_execute_tpch", EXECUTOR_PREFIX + "test_optimized_tpch"},
    "optimizer/tpc-ds/tpc-ds.sql": {EXECUTOR_PREFIX + "test_execute_tpcds"},
}
# Giant tests (>= 9 s): "drop" = release on clean cheap evidence, "probe" = also require a passing probe of
# test_execute_tpcds on a few queries, "keep" = always include.
GIANT_MODE = os.environ.get("BR_GIANT_MODE", "smart")
# "smart" = "drop", except where a giant checks behaviour that no cheap test sees: test_execute_tpcds for the
# executor's own code (planner, Python executor and its code generator), test_max_nodes for the parser's node
# limit.
DROP_GIANTS = GIANT_MODE in ("drop", "smart")
MAXN_TEST = "tests/test_parser.py::TestParser::test_max_nodes"
EXEC_SPECIFIC = ("sqlglot/executor/", "sqlglot/planner.py", "sqlglot/generators/python.py")
MAXN_REGION = ("sqlglot/parser.py", 2190, 2212)  # Parser.validate_expression: node counting and the limit error
# Diagnostics (practice-board bisection): run no differential import (importers fallback) / verify nothing.
NO_DIFF = os.environ.get("BR_NO_DIFF", "0") == "1"
NO_VERIFY = os.environ.get("BR_NO_VERIFY", "0") == "1"
NO_BLOWUP = os.environ.get("BR_NO_BLOWUP", "0") == "1"   # v1 trigger: diff only when it saves enough
P_NONE_CODE = 0.5        # prior: no candidate fails (sample: 8 of 15 code commits with candidates)
P_NONE_DATA = 0.05


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Plan:
    def __init__(self, cid, ca, cand_nums):
        self.cid = cid
        self.ca = ca
        self.cand = cand_nums          # set of test numbers (sound superset)
        self.V = set()
        self.X = set()
        self.est = 0.0
        self.gain = 0.0
        self.verified = None           # {test_id: status} after the engine ran
        self.vcpu = None


def class_setup_costs(ix):
    by_cls = {}
    for i, tid in enumerate(ix.ids):
        f, cls, _ = tid.split("::")
        by_cls.setdefault((f, cls), []).append(ix.e[i])
    return {k: max(0.0, min(v) - 0.004) for k, v in by_cls.items()}


def _may_change_imports(text):
    """Could this patch change which modules `import sqlglot` loads? (import statements touched, or a
    package __init__ edited)."""
    cur = ""
    for line in text.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            cur = line[4:].strip()
            if cur.endswith("__init__.py") and "sqlglot/" in cur:
                return True
            continue
        if line[:1] in "+-" and not line.startswith(("+++", "---")) and "sqlglot/" in cur:
            body = line[1:].strip()
            if body.startswith(("import ", "from ")) or "import_module" in body or "__import__" in body:
                return True
    return False


MIN_EXEC_EVIDENCE = 5


def _exec_specific(ca):
    return any(f.startswith(EXEC_SPECIFIC) for f in ca.files)


def _smart_veto(test_id, ca, exec_pass=None):
    """True if this giant must stay whatever the cheap evidence says. For test_execute_tpcds and a change in
    the executor's own code the evidence must come from the executor's tests: `exec_pass` cheap
    tests/test_executor.py tests verified passing (None while planning: not yet known)."""
    if test_id == PROBE_TEST:
        if not _exec_specific(ca):
            return False
        return exec_pass is not None and exec_pass < MIN_EXEC_EVIDENCE
    if test_id == MAXN_TEST:
        f0, lo, hi = MAXN_REGION
        return any(f == f0 and a <= hi and b >= lo for f, a, b in ca.fn_spans)
    return True  # a giant we have no specific knowledge of


def _affine(test_id, changed_paths):
    meth = test_id.rsplit("::", 1)[-1].lower()
    for p in changed_paths:
        base = os.path.basename(p)
        if base.endswith(".py"):
            base = base[:-3]
        if len(base) >= 5 and base.lower() in meth:
            return True
    return False


def main():
    t0 = time.monotonic()
    tests_doc = json.load(open(os.path.join(DATA, "tests.json"), encoding="utf-8"))
    tests = tests_doc["tests"]
    dur = {t["number"]: int(round(t["seconds"] * 1000)) for t in tests}
    num_of_id = {t["id"]: t["number"] for t in tests}
    id_of_num = {t["number"]: t["id"] for t in tests}
    ix = Index()
    idx_to_num = [num_of_id.get(tid) for tid in ix.ids]
    num_to_idx = {n: i for i, n in enumerate(idx_to_num) if n is not None}
    unknown_nums = sorted(set(dur) - set(num_to_idx))  # tests we have no coverage for
    an = Analyzer(ix, ROOT)
    setup = class_setup_costs(ix)

    def inc(n):
        i = num_to_idx[n]
        f, cls, _ = ix.ids[i].split("::")
        return max(0.002, ix.e[i] - setup.get((f, cls), 0.0)), (f, cls)

    cases_db = [None]

    def get_cases():
        if cases_db[0] is None:
            from .cases import Cases

            cases_db[0] = Cases()
        return cases_db[0]

    cdir = os.path.join(DATA, "commits")
    names = sorted(f for f in os.listdir(cdir) if f.endswith(".patch"))
    plans = []
    for fn in names:
        cid = fn[:-6]
        with open(os.path.join(cdir, fn), encoding="utf-8", errors="surrogateescape", newline="") as fh:
            text = fh.read()
        ca = an.analyze(cid, text)
        cand = {idx_to_num[i] for i in ca.cand if idx_to_num[i] is not None}
        if ca.import_time and _may_change_imports(text):
            for tid in SUBPROCESS_TESTS:
                i = ix.pos.get(tid)
                n = num_of_id.get(tid)
                if i is None or n is None:
                    continue
                if any(i in ix.file_any(fp) for fp in ca.files if fp.startswith("sqlglot/") and fp.endswith(".py")):
                    cand.add(n)
        if unknown_nums and (ca.py_changed or "fallback" in ca.kinds or "fixture" in ca.kinds):
            cand |= set(unknown_nums)
        plans.append(Plan(cid, ca, cand))
    n_commits = max(1, len(plans))
    wall_budget = float(os.environ.get("BR_WALL_BUDGET", min(150.0, 2.4 * n_commits + 20)))
    t1 = time.monotonic()
    log(f"analysis: {len(plans)} commits in {t1 - t0:.2f}s")

    # ---- differential import for import-time changes ----
    want = []
    for p in plans:
        ca = p.ca
        if not ca.needs_diff:
            continue
        fb = {idx_to_num[i] for i in ca.diff_fallback if idx_to_num[i] is not None}
        extra = fb - p.cand
        if not extra:
            continue
        gain = (best_schedule(p.cand | fb, dur)[0] - best_schedule(p.cand, dur)[0]) / 1000.0
        # a much larger candidate set also kills later verification, even when blind cost is equal
        blowup = len(extra) >= 30 and len(extra) > 2 * len(p.cand) and not NO_BLOWUP
        if (gain > DIFF_COST * 1.2 or blowup) and ca.verifiable and not NO_DIFF:
            want.append((p, fb))
        else:
            p.cand |= fb
            ca.notes.append(f"no diff (gain {gain:.2f}s): +{len(extra)} fallback")
    if want:
        from . import diffimport, importtime
        keys = importtime.Keys(ix)
        try:
            keys.aliases = diffimport._load_base().get("aliases", {}) or {}
        except Exception:  # noqa: BLE001
            keys.aliases = {}
        files = [dict(p.ca.files) for p, _ in want]
        res = diffimport.run_diffs(ROOT, files, parallel=2, deadline=t0 + wall_budget * 0.6)
        for (p, fb), (r, cpu) in zip(want, res):
            if r is None:
                p.cand |= fb
                p.ca.notes.append(f"diff failed (cpu {cpu:.2f}s): fallback {len(fb)}")
                continue
            try:
                patched = [m for m in (diffimport.path_to_module(fp) for fp in p.ca.files) if m]
                idents = {}
                for fp, ids in p.ca.changed_idents.items():
                    m = diffimport.path_to_module(fp)
                    if m:
                        idents.setdefault(m, set()).update(ids)
                c = importtime.candidates_from_diff(r, ix, keys, an, ROOT, p.ca.notes, patched, idents,
                                                    fn_spans=list(p.ca.fn_spans))
            except Exception as e:  # noqa: BLE001  never lose a commit to a mapping bug: fall back
                p.cand |= fb
                p.ca.notes.append(f"diff mapping error {e!r}: fallback {len(fb)}")
                continue
            if r.get("lazy_changed"):
                # `import sqlglot` now registers a different number of dialects: the fresh-interpreter test sees it
                c = set(c) | {ix.pos[t] for t in SUBPROCESS_TESTS if t in ix.pos}
                p.ca.notes.append("diff: dialect registry after bare import changed -> subprocess tests")
            cn = {idx_to_num[i] for i in c if idx_to_num[i] is not None}
            p.cand |= cn
            p.ca.notes.append(f"diff cpu {cpu:.2f}s: {len(r.get('changes', []))} changes, "
                              f"{len(r.get('failed', {}))} failed -> +{len(cn)}")
        log(f"diffs: {len(want)} in {time.monotonic() - t1:.2f}s")

    # ---- verification planning ----
    cpu_budget = float(os.environ.get("BR_CPU_BUDGET", 1.5 * n_commits))
    to_verify = []
    heavy_needed = False
    for p in plans:
        try:
            if not p.cand or not p.ca.verifiable or "fallback" in p.ca.kinds:
                continue
            if any(n not in num_to_idx for n in p.cand):
                continue
            blind = best_schedule(p.cand, dur)[0] / 1000.0
            # fixture-driven tests: with a function-body-only change, only the cases reaching it can differ
            p.filters = {}
            p.cost_scale = {}
            case_ok = (p.ca.fn_spans and not p.ca.import_time
                       and p.ca.kinds <= {"py_fn", "py_noop"})
            if case_ok:
                cdb = get_cases()
                for n in p.cand:
                    tid = id_of_num[n]
                    if not cdb.has(tid):
                        continue
                    flt, nrel, ntot = cdb.relevant(tid, p.ca.fn_spans)
                    if flt is not None and ntot:
                        p.filters[tid] = flt
                        p.cost_scale[n] = (nrel + 0.5) / ntot

            def cost_of(n):
                c, key = inc(n)
                sc = p.cost_scale.get(n)
                return (c * sc + 0.01 if sc is not None else c), key

            V, X = set(), set()
            force_x = {idx_to_num[i] for i in p.ca.force_x if idx_to_num[i] is not None}
            for n in p.cand:
                c, _ = cost_of(n)
                if (id_of_num[n] in SUBPROCESS_TESTS or n in force_x or c > max(1.0, 1.25 * dur[n] / 1000.0)
                        or (dur[n] >= GIANT_MS and c > 3.0)):
                    X.add(n)
                else:
                    V.add(n)
            if not V:
                continue

            def est_of(S):
                e = JOB_OVERHEAD
                seen = set()
                hv = False
                for n in S:
                    c, key = cost_of(n)
                    e += c
                    if key not in seen:
                        seen.add(key)
                        e += setup.get(key, 0.0)
                    if key[0] in HEAVY_FILES:
                        hv = True
                return e, hv

            p_none = P_NONE_CODE if p.ca.py_changed else P_NONE_DATA
            giants = {n for n in X if dur[n] >= GIANT_MS}
            if GIANT_MODE == "smart":
                giants = {n for n in giants if not _smart_veto(id_of_num[n], p.ca)}
            p.cost_of = cost_of
            skip_ok_kind = not p.ca.kinds & {"fixture", "other", "py_syntax"}
            # A giant is never dropped blindly. test_execute_tpcds can be *probed*: run it on a few TPC-DS
            # queries that reach the change (or a fixed spread of queries); only a passing probe (plus clean
            # cheap evidence) lets it go. Other giants (test_max_nodes) cannot be probed and always stay.
            p.probe = None
            probe_n = next((n for n in giants if id_of_num[n] == PROBE_TEST), None)
            if (GIANT_MODE == "probe" and probe_n is not None and skip_ok_kind
                    and PROBE_FIXTURE_READER not in {id_of_num[m] for m in V}):
                idx = None
                if probe_n in p.cost_scale and PROBE_TEST in p.filters:
                    rel = sorted(p.filters[PROBE_TEST].get(PROBE_FIXTURE, ()))
                    if rel:
                        idx = [rel[int(i * (len(rel) - 1) / max(1, PROBE_K - 1))] for i in range(min(PROBE_K, len(rel)))]
                if idx is None:
                    idx = list(PROBE_DEFAULT)
                idx = sorted(set(idx))
                p.probe = (probe_n, idx)
                p.filters[PROBE_TEST] = {PROBE_FIXTURE: set(idx)}
                p.cost_scale[probe_n] = (len(idx) + 0.5) / 99.0
                V = V | {probe_n}
                X = X - {probe_n}
            giants = giants - ({p.probe[0]} if p.probe else set())
            # FULL: verify all of V.  LITE: verify only tests whose local cost is well below their recorded
            # cost (plus enough cheap tests to carry evidence); include the rest of V unverified.
            lite = {n for n in V if cost_of(n)[0] <= 0.6 * dur[n] / 1000.0 + 0.003}
            cheap_lite = [n for n in lite if dur[n] < CHEAP_MS]
            if len(cheap_lite) < MIN_CHEAP_EVIDENCE:
                extra = sorted((n for n in V - lite if dur[n] < CHEAP_MS), key=lambda n: cost_of(n)[0])
                lite |= set(extra[: MIN_CHEAP_EVIDENCE * 2 - len(cheap_lite)])
            options = []
            # an executor change can only release test_execute_tpcds on the executor tests' own evidence: verify all
            only_full = GIANT_MODE == "smart" and _exec_specific(p.ca) and any(id_of_num[n] == PROBE_TEST for n in giants)
            for label, S in (("full", V), ("lite", lite)):
                if not S or (label == "lite" and (S == V or only_full)):
                    continue
                est, heavy = est_of(S)
                B = V - S
                if p.probe is not None and label == "lite":
                    S = S | {p.probe[0]}
                    B = V - S
                small = sum(1 for n in S if dur[n] < CHEAP_MS)
                can_skip = bool(giants) and small >= MIN_CHEAP_EVIDENCE and skip_ok_kind and DROP_GIANTS
                keep_none = B | ((X - giants) if can_skip else X)
                if p.probe is not None and small < MIN_CHEAP_EVIDENCE:
                    keep_none = keep_none | {p.probe[0]}
                cost_none = best_schedule(keep_none, dur)[0] / 1000.0
                some = min(S, key=lambda n: dur[n])
                cost_fail = best_schedule(X | B | {some}, dur)[0] / 1000.0
                expected = est + p_none * cost_none + (1 - p_none) * cost_fail
                gain = blind - expected - (HEAVY_PRELOAD if heavy and not heavy_needed else 0.0)
                options.append((gain, label, S, est, heavy, cost_none, cost_fail))
            if os.environ.get("BR_VERBOSE"):
                p.ca.notes.append("options: " + ", ".join(f"{o[1]} gain {o[0]:.2f} est {o[3]:.2f} none {o[5]:.2f}" for o in options))
            gain, label, S, est, heavy, cost_none, cost_fail = max(options, key=lambda o: o[0])
            p.V, p.X, p.est, p.gain = S, X, est, gain
            p.ca.notes.append(f"plan[{label}]: blind {blind:.2f} est {est:.2f} |S|={len(S)} |V|={len(V)} |X|={len(X)} "
                              f"giants={len(giants)} cost_none {cost_none:.2f} cost_fail {cost_fail:.2f} gain {gain:.2f}")
            if p.gain > 0.1 and not NO_VERIFY:
                to_verify.append(p)
                heavy_needed = heavy_needed or heavy
        except Exception as e:  # noqa: BLE001  planning bug: this commit simply is not verified
            p.V, p.X = set(), set()
            p.ca.notes.append(f"planning error {e!r}: not verified")
    to_verify.sort(key=lambda p: -p.gain / max(p.est, 0.05))
    chosen, spent = [], 0.0
    for p in to_verify:
        if spent + p.est > cpu_budget:
            continue
        chosen.append(p)
        spent += p.est
    log(f"verify {len(chosen)} commits, est cpu {spent:.1f}s")

    if chosen:
        from . import engine
        os.environ.setdefault("SKIP_INTEGRATION", "0")
        if any(id_of_num[n].split("::")[0] in HEAVY_FILES for p in chosen for n in p.V):
            try:
                engine.preload()
            except Exception as e:  # noqa: BLE001
                log("preload failed", repr(e))
        deadline = t0 + wall_budget
        jobs = []
        for p in chosen:
            # cheapest classes first, cheapest tests first within a class (setUpClass runs once), so a
            # failure (which ends the job early) is found for as little CPU as possible
            by_cls = {}
            for n in p.V:
                c, key = p.cost_of(n)
                by_cls.setdefault(key, []).append((c, n))
            keys_sorted = sorted(by_cls, key=lambda k: setup.get(k, 0.0) + min(x[0] for x in by_cls[k]))
            ids = []
            for k in keys_sorted:
                ids.extend(id_of_num[n] for _, n in sorted(by_cls[k]))
            per = {}
            always = []
            # Once a test fails the job skips the rest (kept unverified), except tests whose removal would cut
            # the commit's cost by more than verifying them costs, given what stays selected anyway on failure
            # (every unverified candidate plus a failing test).
            kept_on_fail = set(p.cand) - set(p.V)
            if p.V:
                kept_on_fail.add(min(p.V, key=lambda n: dur[n]))
            base_ms = best_schedule(kept_on_fail, dur)[0] if kept_on_fail else 0
            for n in p.V:
                c, key = p.cost_of(n)
                per[id_of_num[n]] = max(2.0, 5.0 * c + 1.0)
                if (c < 0.5 * dur[n] / 1000.0 and dur[n] >= 300 and not (p.probe and n == p.probe[0])
                        and n not in kept_on_fail):
                    gain_ms = best_schedule(kept_on_fail | {n}, dur)[0] - base_ms
                    # its class may not be set up yet when the failure happens: count setUpClass too
                    if gain_ms / 1000.0 > 1.2 * (c + setup.get(key, 0.0)) + 0.3:
                        always.append(id_of_num[n])
            # A giant left unverified (too costly to run blindly) is worth running once the change is known to
            # break something - it is the busiest worker then - if dropping it cuts the commit's cost by more
            # than running its relevant cases costs. The engine runs these last, and only after a failure.
            p.after_fail = set()
            fx_nums = {idx_to_num[i] for i in p.ca.force_x if idx_to_num[i] is not None}
            for n in sorted(set(p.cand) - set(p.V)):
                if (dur[n] < GIANT_MS or (p.probe and n == p.probe[0]) or n in fx_nums
                        or id_of_num[n] in SUBPROCESS_TESTS or n not in p.cost_scale):
                    continue
                c, key = p.cost_of(n)
                gain_ms = base_ms - best_schedule(kept_on_fail - {n}, dur)[0]
                if gain_ms / 1000.0 > 1.2 * (c + setup.get(key, 0.0)) + 0.5:
                    p.after_fail.add(n)
                    per[id_of_num[n]] = max(2.0, 5.0 * c + 1.0)
                    p.est += c + setup.get(key, 0.0)  # the job's time budget derives from est
            # merge case filters per fixture; only when every reader of that fixture in this job is filtered
            vids = {id_of_num[n] for n in p.V | p.after_fail}
            merged = {}
            blocked = set()
            for tid in vids:
                for fx, keep in p.filters.get(tid, {}).items():
                    merged.setdefault(fx, set()).update(keep)
            for fx in list(merged):
                users = CLASS_LOADED_FIXTURES.get(fx)
                for ri in ix.readers("tests/fixtures/" + fx):
                    rn = idx_to_num[ri]
                    rt = id_of_num.get(rn) if rn is not None else None
                    if users is not None and rt and rt.startswith(EXECUTOR_PREFIX) and rt not in users:
                        continue  # opened by the class's setUpClass only; this test never uses the list
                    if rt in vids and fx not in p.filters.get(rt, {}):
                        blocked.add(fx)
                for tid in vids:
                    if tid not in p.filters and fx in get_cases().fixtures(tid):
                        blocked.add(fx)
            case_filters = {fx: sorted(v) for fx, v in merged.items() if fx not in blocked}
            for n in list(p.after_fail):
                if any(fx not in case_filters for fx in p.filters.get(id_of_num[n], {})):
                    p.after_fail.discard(n)  # its filter is blocked: it would run in full
            if case_filters:
                p.ca.notes.append(f"case filters: " + ", ".join(f"{fx}:{len(v)}" for fx, v in case_filters.items()))
            af_ids = [id_of_num[n] for n in sorted(p.after_fail)]
            if af_ids:
                p.ca.notes.append(f"after-failure giants: {af_ids}")
            jobs.append((p, ids + af_ids, per, {"always": always, "stop_after_fail": True, "case_filters": case_filters,
                                                "after_fail": af_ids}))
        stats = engine.run_many(ROOT, jobs, deadline, parallel=2, log=log)
        for job, (cpu, wall) in zip(jobs, stats):
            job[0].vcpu = cpu

    # ---- final selections ----
    out = []
    total_ms = 0
    for p in plans:
        sel = set(p.cand)
        optional = set()
        try:
            if p.verified is not None and set(p.verified) >= {id_of_num[n] for n in p.V}:
                keep = set()
                bad = False
                af = getattr(p, "after_fail", set())
                af_pass = set()
                for tid, st in p.verified.items():
                    n = num_of_id.get(tid)
                    if n is None:
                        continue
                    if n in af:
                        # run only after a failure: not run = unverified as before; passed = drop; else keep
                        if st in ("pass", "skip"):
                            af_pass.add(n)
                        elif st != "unknown":
                            keep.add(n)
                        continue
                    if st in ("pass", "skip"):
                        continue
                    keep.add(n)
                    if st != "fail":
                        bad = True  # timeout / unknown: evidence incomplete
                if p.probe is not None:
                    pn = p.probe[0]
                    pst = p.verified.get(id_of_num[pn])
                    cheap_ok = sum(1 for n in p.V if n != pn and dur[n] < CHEAP_MS) >= MIN_CHEAP_EVIDENCE
                    anomaly0 = p.vcpu is not None and p.vcpu > 3.0 * p.est + 0.5
                    if (keep - {pn}) or pst != "pass" or not cheap_ok or bad or anomaly0:
                        keep.add(pn)  # the giant stays unless its probe passed on clean evidence
                    else:
                        p.ca.notes.append(f"giant {pn} released: probe on {len(p.probe[1])} queries passed")
                unverified = p.cand - p.V - af_pass
                if af_pass:
                    p.ca.notes.append(f"giants released after the failure: {sorted(af_pass)} passed")
                giants = {n for n in unverified if dur[n] >= GIANT_MS}
                anomaly = p.vcpu is not None and p.vcpu > 3.0 * p.est + 0.5
                small_v = sum(1 for n in p.V if dur[n] < CHEAP_MS)
                changed = list(p.ca.py_changed)
                skip_giants = (DROP_GIANTS and giants and not keep and not bad and not anomaly and small_v >= MIN_CHEAP_EVIDENCE
                               and not p.ca.kinds & {"fixture", "other", "py_syntax"}
                               and not any(_affine(id_of_num[n], changed) for n in giants))
                if skip_giants and GIANT_MODE == "smart":
                    exec_pass = sum(1 for n in p.V if dur[n] < CHEAP_MS and id_of_num[n].startswith(EXECUTOR_PREFIX)
                                    and p.verified.get(id_of_num[n]) == "pass")
                    veto = {n for n in giants if _smart_veto(id_of_num[n], p.ca, exec_pass)}
                    if veto:
                        p.ca.notes.append(f"giants kept (smart veto): {sorted(veto)}")
                    giants -= veto
                    skip_giants = bool(giants)
                if skip_giants:
                    unverified -= giants
                    optional |= giants
                    p.ca.notes.append(f"dropped giants {sorted(giants)} on evidence: {small_v} cheap tests pass")
                sel = keep | unverified
        except Exception as e:  # noqa: BLE001  a bug here must not cost a commit: keep every candidate
            sel, optional = set(p.cand), set()
            p.ca.notes.append(f"final selection error {e!r}: all candidates")
        cost, bins = best_schedule(sel, dur)
        if optional and bins:
            bins, _ = fill_slack(bins, dur, optional)
        total_ms += cost
        out.append(json.dumps({"commit": p.cid, "workers": bins}, separators=(",", ":")))
        log(f"{p.cid}: kinds={sorted(p.ca.kinds)} cand={len(p.cand)} final={len(sel)} cost={cost/1000:.2f}s"
            + (" verified" if p.verified is not None else ""))
        if os.environ.get("BR_VERBOSE"):
            for note in p.ca.notes:
                log("    " + note)
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    sys.stdout.flush()
    log(f"total simulated {total_ms/1000:.1f}s, wall {time.monotonic() - t0:.2f}s")


_WROTE = [False]


def _safe_fallback(exc):
    """Last line of defence: never let a bug reject the run. Re-run the (sound) static analysis alone,
    and if even that fails, select every test for every commit."""
    import traceback

    log("FATAL in selector, falling back:", repr(exc))
    if _WROTE[0]:
        return
    traceback.print_exc(file=sys.stderr)
    tests = json.load(open(os.path.join(DATA, "tests.json"), encoding="utf-8"))["tests"]
    dur = {t["number"]: int(round(t["seconds"] * 1000)) for t in tests}
    num_of_id = {t["id"]: t["number"] for t in tests}
    cdir = os.path.join(DATA, "commits")
    names = sorted(f for f in os.listdir(cdir) if f.endswith(".patch"))
    try:
        ix = Index()
        an = Analyzer(ix, ROOT)
        idx_to_num = [num_of_id.get(tid) for tid in ix.ids]
    except Exception:  # noqa: BLE001
        an = None
    out = []
    for fn in names:
        sel = set(dur)
        if an is not None:
            try:
                with open(os.path.join(cdir, fn), encoding="utf-8", errors="surrogateescape", newline="") as fh:
                    ca = an.analyze(fn[:-6], fh.read())
                c = {idx_to_num[i] for i in (ca.cand | ca.diff_fallback) if idx_to_num[i] is not None}
                known = {n for n in idx_to_num if n is not None}
                sel = c | (set(dur) - known)
            except Exception:  # noqa: BLE001
                sel = set(dur)
        _, bins = best_schedule(sel, dur)
        out.append(json.dumps({"commit": fn[:-6], "workers": bins}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except BaseException as e:  # noqa: BLE001
        if isinstance(e, SystemExit) and not e.code:
            raise
        _safe_fallback(e)
