# Trace line coverage inside subprocesses (spawned pool workers) when BR_TRACE_DIR is set.
import os
import sys

_d = os.environ.get("BR_TRACE_DIR")
if _d:
    import atexit
    import json

    _mon = sys.monitoring
    _tool = _mon.COVERAGE_ID
    _lines = {}
    _top = {}
    _ROOT = "/sqlglot/"

    def _on_line(code, line):
        f = code.co_filename
        if f.startswith(_ROOT):
            d = _lines if code.co_flags & 1 else _top
            s = d.get(f)
            if s is None:
                s = d[f] = set()
            s.add(line)
        return _mon.DISABLE

    _first = {}

    def _on_start(code, offset):
        f = code.co_filename
        if f.startswith(_ROOT):
            ln = _first.get(code)
            if ln is None:
                ln = 0
                for _s, _e, l in code.co_lines():
                    if l is not None:
                        ln = l
                        break
                _first[code] = ln
            s = _lines.get(f)
            if s is None:
                s = _lines[f] = set()
            s.add(ln)
        return _mon.DISABLE

    try:
        _mon.use_tool_id(_tool, "cov")
        _mon.register_callback(_tool, _mon.events.LINE, _on_line)
        _mon.register_callback(_tool, _mon.events.PY_START, _on_start)
        _mon.set_events(_tool, _mon.events.LINE | _mon.events.PY_START)
    except ValueError:
        pass

    def _dump():
        try:
            _mon.set_events(_tool, 0)
            os.makedirs(_d, exist_ok=True)
            with open(os.path.join(_d, "w%d.json" % os.getpid()), "w") as fh:
                json.dump({"fn": {f: sorted(s) for f, s in _lines.items()}, "top": {f: sorted(s) for f, s in _top.items()}}, fh)
        except Exception:
            pass

    atexit.register(_dump)
