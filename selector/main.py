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
JOB_OVERHEAD = 0.12      # s CPU: fork + fresh sqlglot import + overlay compile (child)
DIFF_COST = 1.0          # s CPU: one differential import (typical)
HEAVY_PRELOAD = 0.70     # s CPU once: duckdb/pandas/numpy in the zygote
GIANT_MS = 9000          # tests this long are never verified; they may be dropped on strong evidence
CHEAP_MS = 900
MIN_CHEAP_EVIDENCE = 5
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

    cdir = os.path.join(DATA, "commits")
    names = sorted(f for f in os.listdir(cdir) if f.endswith(".patch"))
    plans = []
    for fn in names:
        cid = fn[:-6]
        with open(os.path.join(cdir, fn), encoding="utf-8", errors="surrogateescape", newline="") as fh:
            text = fh.read()
        ca = an.analyze(cid, text)
        cand = {idx_to_num[i] for i in ca.cand if idx_to_num[i] is not None}
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
        if gain > DIFF_COST * 1.2 and ca.verifiable:
            want.append((p, fb))
        else:
            p.cand |= fb
            ca.notes.append(f"no diff (gain {gain:.2f}s): +{len(extra)} fallback")
    if want:
        from . import diffimport, importtime
        keys = importtime.Keys(ix)
        files = [dict(p.ca.files) for p, _ in want]
        res = diffimport.run_diffs(ROOT, files, parallel=2, deadline=t0 + wall_budget * 0.6)
        for (p, fb), (r, cpu) in zip(want, res):
            if r is None:
                p.cand |= fb
                p.ca.notes.append(f"diff failed (cpu {cpu:.2f}s): fallback {len(fb)}")
                continue
            c = importtime.candidates_from_diff(r, ix, keys, an, ROOT, p.ca.notes)
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
        if not p.cand or not p.ca.verifiable or "fallback" in p.ca.kinds:
            continue
        if any(n not in num_to_idx for n in p.cand):
            continue
        blind = best_schedule(p.cand, dur)[0] / 1000.0
        V, X = set(), set()
        for n in p.cand:
            c, _ = inc(n)
            if dur[n] >= GIANT_MS or c > max(1.0, 1.25 * dur[n] / 1000.0):
                X.add(n)
            else:
                V.add(n)
        if not V:
            continue
        est = JOB_OVERHEAD
        seen = set()
        heavy = False
        for n in V:
            c, key = inc(n)
            est += c
            if key not in seen:
                seen.add(key)
                est += setup.get(key, 0.0)
            if key[0] in HEAVY_FILES:
                heavy = True
        p_none = P_NONE_CODE if p.ca.py_changed else P_NONE_DATA
        giants = {n for n in X if dur[n] >= GIANT_MS}
        small_v = sum(1 for n in V if dur[n] < CHEAP_MS)
        can_skip = bool(giants) and small_v >= MIN_CHEAP_EVIDENCE and not p.ca.kinds & {"fixture", "other", "py_syntax"}
        x_if_none = (X - giants) if can_skip else X
        cost_none = best_schedule(x_if_none, dur)[0] / 1000.0
        some = min(V, key=lambda n: dur[n])
        cost_fail = best_schedule(X | {some}, dur)[0] / 1000.0
        expected = est + p_none * cost_none + (1 - p_none) * cost_fail
        p.V, p.X, p.est = V, X, est
        p.gain = blind - expected - (HEAVY_PRELOAD if heavy and not heavy_needed else 0.0)
        if p.gain > 0.1:
            to_verify.append(p)
            heavy_needed = heavy_needed or heavy
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
            ids = [id_of_num[n] for n in sorted(p.V)]
            per = {}
            for n in p.V:
                c, _ = inc(n)
                per[id_of_num[n]] = max(2.0, 5.0 * c + 1.0)
            jobs.append((p, ids, per))
        stats = engine.run_many(ROOT, jobs, deadline, parallel=2, log=log)
        for (p, _, _), (cpu, wall) in zip(jobs, stats):
            p.vcpu = cpu

    # ---- final selections ----
    out = []
    total_ms = 0
    for p in plans:
        sel = set(p.cand)
        optional = set()
        if p.verified is not None:
            keep = set()
            bad = False
            for tid, st in p.verified.items():
                n = num_of_id.get(tid)
                if n is None:
                    continue
                if st in ("pass", "skip"):
                    continue
                keep.add(n)
                if st != "fail":
                    bad = True  # timeout / unknown: evidence incomplete
            unverified = p.cand - p.V
            giants = {n for n in unverified if dur[n] >= GIANT_MS}
            anomaly = p.vcpu is not None and p.vcpu > 3.0 * p.est + 0.5
            small_v = sum(1 for n in p.V if dur[n] < CHEAP_MS)
            changed = list(p.ca.py_changed)
            skip_giants = (giants and not keep and not bad and not anomaly and small_v >= MIN_CHEAP_EVIDENCE
                           and not p.ca.kinds & {"fixture", "other", "py_syntax"}
                           and not any(_affine(id_of_num[n], changed) for n in giants))
            if skip_giants:
                unverified -= giants
                optional |= giants
                p.ca.notes.append(f"dropped giants {sorted(giants)} on evidence: {small_v} cheap tests pass")
            sel = keep | unverified
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


if __name__ == "__main__":
    main()
