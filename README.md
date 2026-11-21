# Blast Radius: minimum-cost sound test selection for sqlglot

Builders Buildathon, RWX track. Given a patch to [sqlglot](https://github.com/tobymao/sqlglot) at commit
`eb11aec`, print which tests to run and how to split them across at most 8 workers.

## 1. The problem, stated exactly

Let $T$ be the test set ($|T| = 1620$) with recorded durations $d_t \in \mathbb{N}$ (milliseconds).
A commit $c$ has an unknown failing set $F_c \subseteq T$. For each commit we output a selection
$S_c \subseteq T$ and a partition $S_c = B_1 \sqcup \dots \sqcup B_k$, $k \le 8$. Its cost is

$$
\mathrm{cost}(c) \;=\; [S_c \neq \emptyset]\Big(\max_{j \le k} \sum_{t \in B_j} d_t \;+\; 2000\,k\Big) \;+\; 300\,000\cdot[F_c \not\subseteq S_c]
$$

and a run scores $\sum_c \mathrm{cost}(c) + \mathrm{CPU}$, where CPU is every process in the container, and
running sqlglot's own tests to decide is allowed but paid for.

Write $P(S) = \min_{k,\;\text{partition}} \big(\max_j \sum_{B_j} d_t + 2000k\big)$ for the best schedule of a set.
Because the miss penalty (300 s) exceeds the cost of running *every* test on 8 workers (≈ 30 s), any
selection with a non-trivial chance of a miss is dominated. The problem therefore splits into:

1. **Soundness.** Construct $C_c \supseteq F_c$ without seeing $F_c$.
2. **Sharpening.** Shrink $C_c$ towards $F_c$, by analysis or by running tests ourselves, paying CPU
   $e_t$ per test run locally. The floor is the oracle $\sum_c P(F_c)$.
3. **Packing.** Compute $P(S)$ exactly or near-exactly, and use the schedule's slack for free insurance.

## 2. Soundness: what can a change possibly affect?

**Function bodies.** Execution of test $t$ on the patched tree is identical to the pinned tree until
$t$ first executes a changed statement. So $t$ can only fail if it executes it. We record, for every
test **run in isolation** (fresh interpreter per test, so caches and lazy imports are attributed to the
test that needs them; pool workers traced too), every executed line split into *function-code* and
*import-time* lines. A change maps to the innermost enclosing statement (its full line span, or the
header span of a compound statement, so short-circuit operands and multi-line expressions are
covered), and $C$ is the set of tests that executed any line of it.

**Import-time code** (module and class bodies, decorators, defaults) runs once and leaves state behind.
Its effect is fully described by *which state differs*. We run a **differential import**:
- the image build stores fingerprints of every module global and class attribute of the pinned tree
  (stable digests, line-number independent, per key for dicts/sets);
- per commit, a forked child imports the patched modules plus every module that can derive state from
  them (reverse import closure), and diffs.

This catches derived state (`arg_types` → `required_args`, `_sql_names` → every parser's `FUNCTIONS`)
and modules that no longer import. Each changed item maps to the tests that can observe it:

| changed state | tests that can observe it |
| --- | --- |
| module fails to import | every test that imported it |
| `dict`/`set` entry `k` of attribute `A` | tests that read key `k` of `A` at run time, or iterated `A` |
| function object | tests that call it (run-time coverage of its body) |
| other value | tests that run a run-time line mentioning its name |

Per-key reads come from a second offline trace that swaps every class/module-level container for a
recording subclass and runs each test.

**Data files.** A fixture can only affect the tests that opened it (audit hook). A `.sql` fixture edit
confined to `--` comments or blank lines is invisible through the test helpers.

**No-ops.** Comment and formatting edits (comment-only lines outside strings, or an AST-identical
enclosing statement) affect nothing.

## 3. Sharpening by running the candidates

For a commit with candidate set $C$, define the blind cost $B = P(C)$ and the local verification cost
$E(C) = \sum_{t \in C} e_t + \text{setup} + \text{overhead}$. Verification returns $F$ exactly, at
expected cost $E(C) + \mathbb{E}[P(F)]$. We verify when the expected saving is positive:

$$
p_0\,B + (1-p_0)\,\rho\,(B - 2) - E(C) > 0
$$

with $p_0$ the prior probability that nothing in $C$ fails and $\rho$ the expected shrink otherwise.

Verification is in-process and faithful. A zygote forks one child per commit. The child serves the
patched files from memory through an import hook plus an `open()` redirect, imports sqlglot fresh, and
runs the chosen tests with `unittest`. Process pools are replaced by an inline executor that still
pickles every task and result, because the serde path breaks tests. On all 30 sample commits this
reproduces the official failing sets exactly.

## 4. Packing

For each $k \le 8$: an exact 2-way split by subset-sum bitset DP, LPT otherwise, with lower bound
$\max(\max_t d_t, \lceil \sum d_t / k \rceil)$ pruning. The minimum over $k$ of makespan $+ 2000k$ is
taken.

## 5. Layout

```
Dockerfile            pinned base image, sqlglot @ eb11aec, requirements.lock, build-time caches
selector/main.py      orchestration: analysis -> differential import -> verification -> packing
selector/analysis.py  patch -> sound candidate set
selector/diffimport.py, fingerprint.py, importtime.py   differential import
selector/engine.py    local verification engine
selector/pack.py      schedule optimiser
data/                 coverage map, key-access map, import-time lines (all built from the pinned commit)
tools/                the tracers and builders that produced data/, and local run scripts
```

Every map in `data/` is built from the pinned commit only (coverage, key reads, imports), as the rules
allow. Nothing is looked up by patch.

## License

MIT
