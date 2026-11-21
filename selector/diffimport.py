"""Differential import: which import-time state does a patch change?

build_snapshot() runs at image build: imports the pinned sqlglot (every dialect and submodule) and stores
fingerprints of all module globals and class attributes in data/state.marshal.

run_diff() runs per commit at selection time in a forked child: installs the patch overlay, imports the
patched tree the same way, fingerprints it and returns the differences plus the modules that now fail to
import. Children of the selector inherit PYTHONHASHSEED=0 (set in the image) so set-ordering-dependent
state is reproducible between build and run.
"""
import json
import marshal
import os
import select
import signal
import sys
import time

from . import fingerprint as F

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(os.path.dirname(HERE), "data", "state.marshal")


def _prepare(root):
    if root not in sys.path:
        sys.path.insert(0, root)
    os.chdir(root)
    import unittest  # noqa: F401  (sqlglot checks for it at import; engine children have it loaded)
    import unittest.mock  # noqa: F401


def build_snapshot(root="/sqlglot", path=STATE_PATH, noisy=None):
    _prepare(root)
    failed = F.import_all()
    snap = F.snapshot(noisy=noisy)
    deps = F.module_deps()
    rdeps = {}
    for n, ds in deps.items():
        for m in ds:
            rdeps.setdefault(m, []).append(n)
    with open(path, "wb") as fh:
        marshal.dump({"snap": snap, "failed": failed, "rdeps": rdeps, "noisy": sorted(noisy or ())}, fh)
    return sum(len(v) for v in snap.values()), failed


def noisy_tags(path_a, path_b):
    a = marshal.load(open(path_a, "rb"))["snap"]
    b = marshal.load(open(path_b, "rb"))["snap"]
    out = set()
    for m in set(a) | set(b):
        ea, eb = a.get(m, {}), b.get(m, {})
        for tag in set(ea) | set(eb):
            if ea.get(tag) != eb.get(tag):
                out.add(tag)
    return out


def affected_modules(base, modules):
    rdeps = base["rdeps"]
    seen = set(modules)
    work = list(modules)
    while work:
        m = work.pop()
        for n in rdeps.get(m, ()):
            if n not in seen:
                seen.add(n)
                work.append(n)
    return seen


def path_to_module(rel):
    if not rel.endswith(".py"):
        return None
    m = rel[:-3].replace("/", ".")
    if m.endswith(".__init__"):
        m = m[: -len(".__init__")]
    return m


def _child(root, files, w, tmpdir):
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        import importlib

        from .engine import _install_overlay

        with open(STATE_PATH, "rb") as fh:
            base = marshal.load(fh)
        os.makedirs(tmpdir, exist_ok=True)
        _install_overlay(root, files, os.path.join(tmpdir, "d%d" % os.getpid()))
        _prepare(root)
        changed_mods = [m for m in (path_to_module(p) for p in files) if m and m.startswith("sqlglot")]
        affected = affected_modules(base, changed_mods)
        failed = {}
        try:
            import sqlglot  # noqa: F401
        except BaseException as e:  # noqa: BLE001
            failed["sqlglot"] = repr(e)
        if not failed:
            for m in sorted(affected):
                if m in base["failed"]:
                    continue
                try:
                    importlib.import_module(m)
                except BaseException as e:  # noqa: BLE001
                    failed[m] = repr(e)
        snap = F.snapshot(only=affected, noisy=set(base.get("noisy", ())))
        changes = []
        for m in sorted(affected):
            if m in failed:
                continue
            for tag, key, typ in F.diff(base["snap"].get(m, {}), snap.get(m, {})):
                changes.append((m, tag, key, typ))
        new_failed = {m: e for m, e in failed.items() if m not in base["failed"]}
        subs = {}
        for _m, tag, _k, _t in changes:
            if not tag.startswith("C:sqlglot.expressions"):
                continue
            qual = tag[2:].rsplit(":", 1)[0]
            if qual in subs:
                continue
            cls = None
            parts = qual.split(".")
            for i in range(len(parts), 0, -1):
                mm = sys.modules.get(".".join(parts[:i]))
                if mm is not None:
                    try:
                        cls = mm
                        for part in parts[i:]:
                            cls = getattr(cls, part)
                    except AttributeError:
                        cls = None
                    break
            if isinstance(cls, type):
                seen, work = set(), [cls]
                while work:
                    c = work.pop()
                    q = f"{c.__module__}.{c.__qualname__}"
                    if q in seen:
                        continue
                    seen.add(q)
                    work.extend(c.__subclasses__())
                subs[qual] = sorted(seen)
        data = json.dumps({"changes": changes, "failed": new_failed, "subclasses": subs}).encode()
        view = memoryview(data)
        while view:
            n = os.write(w, view)
            view = view[n:]
    finally:
        os._exit(0)


def run_diffs(root, file_sets, parallel=2, timeout=25.0, tmpdir="/tmp/brdiff", deadline=None):
    """Run several differential imports, `parallel` at a time. Returns [(result_or_None, cpu)]."""
    results = [(None, 0.0)] * len(file_sets)
    pending = list(range(len(file_sets)))
    running = {}
    while pending or running:
        now = time.monotonic()
        while pending and len(running) < parallel and (deadline is None or now < deadline - 2.0):
            k = pending.pop(0)
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(r)
                _child(root, file_sets[k], w, tmpdir)
            os.close(w)
            running[r] = (k, pid, [], now + timeout)
        if not running:
            break
        wait = max(0.0, min(v[3] for v in running.values()) - time.monotonic())
        rl, _, _ = select.select(list(running), [], [], wait)
        now = time.monotonic()
        done = []
        for r in rl:
            b = os.read(r, 1 << 16)
            if b:
                running[r][2].append(b)
            else:
                done.append((r, False))
        for r, v in list(running.items()):
            if v[3] <= now and all(r != d[0] for d in done):
                try:
                    os.kill(v[1], signal.SIGKILL)
                except ProcessLookupError:
                    pass
                done.append((r, True))
        for r, killed in done:
            k, pid, chunks, _ = running.pop(r)
            os.close(r)
            _, _, ru = os.wait4(pid, 0)
            cpu = ru.ru_utime + ru.ru_stime
            res = None
            if not killed:
                try:
                    res = json.loads(b"".join(chunks).decode())
                except ValueError:
                    res = None
            results[k] = (res, cpu)
    return results


def run_diff(root, files, timeout=25.0, tmpdir="/tmp/brdiff"):
    return run_diffs(root, [files], parallel=1, timeout=timeout, tmpdir=tmpdir)[0]
