"""Blast Radius selector: for each patch, print the tests to run and how to split them.

Pipeline per commit c:
  1. Static analysis -> C_c, a sound superset of the failing tests F_c (see analysis.py).
  2. Cost model -> decide whether to *verify* C_c by running those tests locally against the patched
     tree (CPU is charged, but it can shrink the selection to exactly F_c, often to nothing).
  3. Output the minimum-cost schedule of the final selection (pack.py).
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


def log(*a):
    print(*a, file=sys.stderr, flush=True)


HEAVY_FILES = {"tests/test_optimizer.py", "tests/test_executor.py"}
JOB_OVERHEAD = 0.10      # s CPU: fork + fresh sqlglot import + overlay compile (child)
DIFF_COST = 1.0          # s CPU: one differential import (typical)
HEAVY_PRELOAD = 0.70     # s CPU once: duckdb/pandas/numpy in the zygote


class Plan:
    def __init__(self, cid, ca, cand_nums):
        self.cid = cid
        self.ca = ca
        self.cand = cand_nums          # set of test numbers (sound superset)
        self.verify = False
        self.verified = None           # dict number -> status after engine run
        self.final = None


def class_setup_costs(ix, ids_by_num):
    """Estimate per-class setUpClass CPU from isolated per-test costs: min over the class."""
    by_cls = {}
    for i, tid in enumerate(ix.ids):
        f, cls, _ = tid.split("::")
        by_cls.setdefault((f, cls), []).append(ix.e[i])
    return {k: max(0.0, min(v) - 0.004) for k, v in by_cls.items()}


def estimate_verify_cost(nums, ix, num_to_idx, setup):
    """CPU seconds to run these tests in one child (setUpClass once per class)."""
    cost = JOB_OVERHEAD
    seen_cls = set()
    heavy = False
    for n in nums:
        i = num_to_idx.get(n)
        if i is None:
            return float("inf"), False
        tid = ix.ids[i]
        f, cls, _ = tid.split("::")
        key = (f, cls)
        s = setup.get(key, 0.0)
        if key not in seen_cls:
            seen_cls.add(key)
            cost += s
        cost += max(0.002, ix.e[i] - s)
        if f in HEAVY_FILES:
            heavy = True
    return cost, heavy


def main():
    t0 = time.monotonic()
    tests_doc = json.load(open(os.path.join(DATA, "tests.json"), encoding="utf-8"))
    tests = tests_doc["tests"]
    dur = {t["number"]: int(round(t["seconds"] * 1000)) for t in tests}
    num_of_id = {t["id"]: t["number"] for t in tests}
    ix = Index()
    idx_to_num = [num_of_id.get(tid) for tid in ix.ids]
    num_to_idx = {n: i for i, n in enumerate(idx_to_num) if n is not None}
    unknown_nums = sorted(set(dur) - set(num_to_idx))  # tests we have no coverage for
    an = Analyzer(ix, ROOT)
    setup = class_setup_costs(ix, None)

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
    t1 = time.monotonic()
    log(f"analysis: {len(plans)} commits in {t1 - t0:.2f}s")

    # ---- differential import for import-time changes ----
    n_commits0 = max(1, len(plans))
    wall_budget0 = float(os.environ.get("BR_WALL_BUDGET", min(150.0, 2.4 * n_commits0 + 20)))
    want = []
    for p in plans:
        ca = p.ca
        if not ca.needs_diff:
            continue
        fb = {idx_to_num[i] for i in ca.diff_fallback if idx_to_num[i] is not None}
        extra = fb - p.cand
        if not extra:
            continue
        base_cost = best_schedule(p.cand, dur)[0]
        with_fb = best_schedule(p.cand | fb, dur)[0]
        gain = (with_fb - base_cost) / 1000.0
        if gain > DIFF_COST * 1.2 and ca.verifiable:
            want.append((p, fb))
        else:
            p.cand |= fb
            ca.notes.append(f"no diff (gain {gain:.2f}s): +{len(extra)} fallback")
    if want:
        from . import diffimport, importtime
        keys = importtime.Keys(ix)
        files = [{k: v for k, v in p.ca.files.items()} for p, _ in want]
        res = diffimport.run_diffs(ROOT, files, parallel=2, deadline=t0 + wall_budget0 * 0.6)
        for (p, fb), (r, cpu) in zip(want, res):
            if r is None:
                p.cand |= fb
                p.ca.notes.append(f"diff failed (cpu {cpu:.2f}s): fallback {len(fb)}")
                continue
            c = importtime.candidates_from_diff(r, ix, keys, an, ROOT, p.ca.notes)
            cn = {idx_to_num[i] for i in c if idx_to_num[i] is not None}
            p.cand |= cn
            p.ca.notes.append(f"diff cpu {cpu:.2f}s: {len(r.get('changes', []))} changes, {len(r.get('failed', {}))} failed -> +{len(cn)}")
        log(f"diffs: {len(want)} in {time.monotonic() - t1:.2f}s")

    # ---- decide what to verify ----
    n_commits = max(1, len(plans))
    wall_budget = float(os.environ.get("BR_WALL_BUDGET", min(150.0, 2.4 * n_commits + 20)))
    cpu_budget = float(os.environ.get("BR_CPU_BUDGET", 2.0 * n_commits))
    to_verify = []
    heavy_needed = False
    for p in plans:
        if not p.cand:
            continue
        blind, _ = best_schedule(p.cand, dur)
        blind /= 1000.0
        if not p.ca.verifiable or p.ca.kinds & {"fallback"}:
            continue
        est, heavy = estimate_verify_cost(p.cand, ix, num_to_idx, setup)
        code_like = bool(p.ca.py_changed)
        p0 = 0.45 if code_like else 0.04  # prior P(no candidate fails)
        reduce = 0.35 if code_like else 0.05
        gain = p0 * blind + (1 - p0) * reduce * max(0.0, blind - 2.0) - est - (HEAVY_PRELOAD if heavy and not heavy_needed else 0)
        p.est = est
        p.blind = blind
        p.gain = gain
        if gain > 0.05:
            to_verify.append(p)
            heavy_needed = heavy_needed or heavy
    to_verify.sort(key=lambda p: -p.gain / max(p.est, 0.05))
    # respect CPU budget
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
        if heavy_needed:
            try:
                engine.preload()
            except Exception as e:  # noqa: BLE001
                log("preload failed", repr(e))
        deadline = t0 + wall_budget
        jobs = []
        for p in chosen:
            ids = [ix.ids[num_to_idx[n]] for n in sorted(p.cand)]
            per = {}
            for n in p.cand:
                i = num_to_idx[n]
                per[ix.ids[i]] = max(3.0, 6.0 * ix.e[i] + 2.0)
            jobs.append((p, ids, per))
        engine.run_many(ROOT, jobs, deadline, parallel=2, log=log)

    # ---- final selections ----
    out = []
    for p in plans:
        sel = set(p.cand)
        if p.verified is not None:
            id_to_num = num_of_id
            keep = set()
            for tid, st in p.verified.items():
                n = id_to_num.get(tid)
                if n is None:
                    continue
                if st != "pass" and st != "skip":
                    keep.add(n)
            sel = keep
        cost, bins = best_schedule(sel, dur)
        p.final = bins
        out.append(json.dumps({"commit": p.cid, "workers": bins}, separators=(",", ":")))
        log(f"{p.cid}: kinds={sorted(p.ca.kinds)} cand={len(p.cand)} final={len(sel)} cost={cost/1000:.2f}s"
            + (f" verified" if p.verified is not None else ""))
        if os.environ.get("BR_VERBOSE"):
            for n in p.ca.notes:
                log("    " + n)
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    sys.stdout.flush()
    log(f"total wall {time.monotonic() - t0:.2f}s")


if __name__ == "__main__":
    main()
