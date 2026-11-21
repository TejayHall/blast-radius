import marshal, sys
sys.path.insert(0, "/app")
from selector import diffimport as D
base = marshal.load(open("/app/data/state.marshal", "rb"))
for m in ["sqlglot.dialects.snowflake", "sqlglot.dialects.tsql", "sqlglot.parsers.singlestore", "sqlglot.expressions.ddl", "sqlglot.generators.clickhouse", "sqlglot.optimizer.scope", "sqlglot.dialects.dialect", "sqlglot.parser"]:
    a = D.affected_modules(base, [m])
    print(m, len(a), sorted(a)[:12])
print("modules in snapshot", len(base["snap"]))
print("rdeps of sqlglot.dialects:", sorted(base["rdeps"].get("sqlglot.dialects", []))[:20])
