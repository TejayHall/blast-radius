"""Choose the number of workers and the split of a test set to minimise

    cost(S) = max_j load_j + W * k        (W = 2000 ms per worker, k <= 8)

and optionally fill the slack of that schedule with "free" optional tests (they never raise the
makespan, so they cost nothing).
"""

W = 2000
KMAX = 8


def _lpt(durs, k):
    """Longest processing time first. durs: list of (d, item). Returns (makespan, bins)."""
    loads = [0] * k
    bins = [[] for _ in range(k)]
    for d, it in sorted(durs, key=lambda x: -x[0]):
        j = min(range(k), key=loads.__getitem__)
        loads[j] += d
        bins[j].append(it)
    return max(loads), bins, loads


def _two_way_exact(durs):
    """Exact 2-partition minimising max load (subset-sum DP with python big-int bitsets)."""
    total = sum(d for d, _ in durs)
    half = total // 2
    # reachable sums
    reach = 1
    mask = (1 << (half + 1)) - 1
    for d, _ in durs:
        reach = (reach | (reach << d)) & mask
    best = reach.bit_length() - 1  # largest reachable sum <= half
    # reconstruct (greedy backwards over prefix bitsets)
    prefix = [1]
    r = 1
    for d, _ in durs:
        r = (r | (r << d)) & mask
        prefix.append(r)
    a, b = [], []
    s = best
    for i in range(len(durs) - 1, -1, -1):
        d, it = durs[i]
        if s - d >= 0 and (prefix[i] >> (s - d)) & 1:
            a.append(it)
            s -= d
        else:
            b.append(it)
    la = best
    lb = total - best
    return max(la, lb), [a, b], [la, lb]


def best_schedule(items, dur):
    """items: iterable of test numbers; dur: number -> ms. Returns (cost_ms, bins)."""
    items = list(items)
    if not items:
        return 0, []
    durs = [(dur[i], i) for i in items]
    total = sum(d for d, _ in durs)
    dmax = max(d for d, _ in durs)
    best = (total + W, [items], [total])
    for k in range(2, min(KMAX, len(items)) + 1):
        lb = max(dmax, -(-total // k))
        if lb + W * k >= best[0]:
            # adding workers can no longer help (lower bound already worse)
            if dmax + W * k >= best[0]:
                break
            continue
        if k == 2 and len(durs) <= 400 and total <= 4_000_000:
            mk, bins, loads = _two_way_exact(durs)
        else:
            mk, bins, loads = _lpt(durs, k)
        c = mk + W * k
        if c < best[0]:
            best = (c, bins, loads)
    cost, bins, loads = best[0], [b for b in best[1] if b], best[2]
    return cost, bins


def fill_slack(bins, dur, optional):
    """Add optional tests into bins where they fit without raising the makespan. Returns bins and
    the list of optional tests that did not fit."""
    if not bins:
        return bins, list(optional)
    loads = [sum(dur[i] for i in b) for b in bins]
    mk = max(loads)
    left = []
    for i in sorted(optional, key=lambda x: -dur[x]):
        d = dur[i]
        j = min(range(len(bins)), key=loads.__getitem__)
        if loads[j] + d <= mk:
            bins[j].append(i)
            loads[j] += d
        else:
            left.append(i)
    return bins, left
