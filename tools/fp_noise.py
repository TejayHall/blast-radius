import os, sys, pickle
sys.path.insert(0, "/app"); sys.path.insert(0, "/sqlglot"); os.chdir("/sqlglot")
import unittest  # match engine children
from selector import fingerprint as F
F.import_all()
import sqlglot
tags = ["C:sqlglot.dialects.databricks.Databricks:text_type", "C:sqlglot.dialects.hive.Hive:target_type",
        "C:sqlglot.lineage.Node:__setattr__", "M:sqlglot.expressions.core:UNITTEST"]
out = {}
for t in tags:
    kind, rest = t.split(":", 1)
    if kind == "C":
        qual, attr = rest.rsplit(":", 1)
        mod, _, cq = qual.partition(".")
        # find module/class
        import importlib
        parts = qual.split(".")
        for i in range(len(parts), 0, -1):
            try:
                m = importlib.import_module(".".join(parts[:i])); break
            except ImportError:
                continue
        obj = m
        for p in parts[i:]:
            obj = getattr(obj, p)
        v = obj.__dict__[attr]
    else:
        _, mod, name = t.split(":")
        import importlib
        v = getattr(importlib.import_module(mod), name)
    out[t] = repr(F.fp(v))[:600]
import sqlglot.generators.hive as H
v = H.HiveGenerator.TRANSFORMS[sqlglot.exp.Create]
out["hiveCreate"] = repr(F.fp(v))[:1500]
pickle.dump(out, open(sys.argv[1], "wb"))
