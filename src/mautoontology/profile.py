"""Profiling: emit read-only SQL, ingest its results, or run it locally.

Two stages keep warehouse cost proportional to what is actually uncertain:

* **Stage 1 - stats:** one query per dataset computing row count, non-null and
  distinct counts per column (min/max only for numeric and temporal columns,
  never for text, so no personal data is read back).
* **Stage 2 - targeted:** using stage-1 facts, exact uniqueness checks for
  candidate keys, inclusion checks for pruned relationship candidates, and
  value domains for low-cardinality, non-PII categorical columns.

``emit`` returns a plan (JSON) with dialect-correct SQL (sqlglot transpiles the
portable SQL). The agent runs each query with its own warehouse access and
saves ``{query_id: rows}``; ``ingest`` folds the rows into ``profile.json``.
``run`` executes both stages against a local SQLite/DuckDB file read-only.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .propose.candidates import (
    composite_key_candidates,
    is_unique,
    key_candidates,
    relationship_candidates,
)
from .propose.naming import is_id_like
from .propose.pii import classify_name, classify_values
from .readonly import assert_single_select, detect_engine, run_query
from .snapshot import Snapshot
from .sqlutil import canonical_dialect, qualified, quote_ident, transpile

PROFILE_VERSION = 1
_MINMAX_FAMILIES = {"integer", "decimal", "float", "date", "timestamp", "timestamp_tz"}
_VALUE_FAMILIES = {"string", "boolean"}
_MAX_DOMAIN = 50
#: Value-only relationship evidence needs at least this many distinct child values.
MIN_VALUE_ONLY_DISTINCT = 20


def _q(name: str) -> str:
    """Quote for the portable (DuckDB-syntax) SQL; transpile handles the target dialect."""
    return quote_ident(name, "duckdb")


def _table(dataset: str, dialect: str) -> str:
    return qualified(dataset, "duckdb")


#: Dialects with an approximate distinct count (others fall back to exact COUNT(DISTINCT)).
_APPROX_DIALECTS = {"snowflake", "databricks", "spark", "bigquery", "tsql", "trino", "presto", "duckdb", "athena", "clickhouse"}
#: Row-sampling clause per dialect, appended after the (single) table of a stats query.
_SAMPLE_CLAUSE = {
    "duckdb": "USING SAMPLE {p} PERCENT (bernoulli)",
    "snowflake": "TABLESAMPLE BERNOULLI ({p})",
    "databricks": "TABLESAMPLE ({p} PERCENT)",
    "spark": "TABLESAMPLE ({p} PERCENT)",
    "bigquery": "TABLESAMPLE SYSTEM ({p} PERCENT)",
    "tsql": "TABLESAMPLE ({p} PERCENT)",
    "postgres": "TABLESAMPLE BERNOULLI ({p})",
    "trino": "TABLESAMPLE BERNOULLI ({p})",
    "presto": "TABLESAMPLE BERNOULLI ({p})",
    "athena": "TABLESAMPLE BERNOULLI ({p})",
}


def _finalize(sql: str, dialect: str, sample_pct: float | None = None) -> str:
    target = canonical_dialect(dialect)
    out = sql if target in {"", "duckdb"} else transpile(sql, target)
    if target == "tsql":  # sqlglot renders APPROX_COUNT_DISTINCT incorrectly for T-SQL
        out = out.replace("APPROX_DISTINCT(", "APPROX_COUNT_DISTINCT(")
    if sample_pct:
        out = f"{out} {_SAMPLE_CLAUSE[target or 'duckdb'].format(p=float(sample_pct))}"
    # Defence in depth: every emitted query must be exactly one read-only SELECT.
    assert_single_select(out, target or None)
    return out


def sampling_supported(dialect: str | None) -> bool:
    return (canonical_dialect(dialect) or "duckdb") in _SAMPLE_CLAUSE


# --------------------------------------------------------------------- stage 1
def emit_stage1(snapshot: Snapshot, dialect: str | None = None, *, approx: bool = False, sample_pct: float | None = None) -> dict[str, Any]:
    dialect = dialect or snapshot.dialect
    target = canonical_dialect(dialect) or "duckdb"
    approx = approx and target in _APPROX_DIALECTS
    if sample_pct and not sampling_supported(dialect):
        sample_pct = None  # no portable sampling clause (e.g. Redshift, SQLite): full scan, reported honestly
    distinct_fn = "APPROX_COUNT_DISTINCT({c})" if approx else "COUNT(DISTINCT {c})"
    queries = []
    for index, ds in enumerate(sorted(snapshot.datasets, key=lambda d: d.name), start=1):
        if not ds.columns:
            continue
        select = ["COUNT(*) AS row_count"]
        columns_map: dict[str, str] = {}
        for ci, col in enumerate(ds.columns):
            alias = f"c{ci}"
            columns_map[alias] = col.name
            c = _q(col.name)
            select.append(f"COUNT({c}) AS {alias}_nn")
            if col.family not in {"json", "array", "binary"}:
                select.append(f"{distinct_fn.format(c=c)} AS {alias}_nd")
            if col.family in _MINMAX_FAMILIES:
                select.append(f"MIN({c}) AS {alias}_min")
                select.append(f"MAX({c}) AS {alias}_max")
        sql = f"SELECT {', '.join(select)} FROM {_table(ds.name, dialect)}"
        queries.append(
            {"id": f"s1_{index:03d}", "kind": "stats", "dataset": ds.name, "columns_map": columns_map,
             "sampled": bool(sample_pct), "approx": approx, "sql": _finalize(sql, dialect, sample_pct)}
        )
    return {"stage": 1, "dialect": dialect, "queries": queries}


# --------------------------------------------------------------------- stage 2
def emit_stage2(
    snapshot: Snapshot,
    profile: dict[str, Any],
    dialect: str | None = None,
    *,
    budget: int = 400,
    value_fallback: bool = True,
) -> dict[str, Any]:
    dialect = dialect or snapshot.dialect
    queries: list[dict[str, Any]] = []
    counter = 0

    def add(entry: dict[str, Any]) -> None:
        nonlocal counter
        counter += 1
        entry["id"] = f"s2_{counter:03d}"
        queries.append(entry)

    # Exact uniqueness for key candidates that stage 1 could not settle exactly.
    keys = key_candidates(snapshot, profile)
    for key in keys:
        ds_profile = (profile.get("datasets") or {}).get(key.dataset) or {}
        exact = not ds_profile.get("approx") and not ds_profile.get("sampled")
        if len(key.columns) == 1 and exact and is_unique(profile, key.dataset, key.columns) is not None:
            continue
        if len(key.columns) == 1 and not _approx_unique(profile, key.dataset, key.columns[0]):
            continue
        add(_uniqueness_query(key.dataset, key.columns, dialect))
    # Composite keys for tables with no single-column key.
    for ds in snapshot.datasets:
        single = [k for k in keys if k.dataset == ds.name and len(k.columns) == 1 and _approx_unique(profile, ds.name, k.columns[0])]
        if single or ds.primary_key:
            continue
        for pair in composite_key_candidates(ds):
            add(_uniqueness_query(ds.name, pair, dialect))

    # Inclusion checks for relationship candidates (parents must look unique).
    rels = relationship_candidates(snapshot, [k for k in keys if k.proven is not False], value_fallback=value_fallback)
    for rel in rels:
        if len(rel.child_columns) != 1:
            continue
        if not rel.declared and not _approx_unique(profile, rel.parent, rel.parent_columns[0]):
            continue
        weak = rel.name_score < 0.5 and "self-reference" not in rel.reason
        child_distinct = profile_column(profile, rel.child, rel.child_columns[0]).get("distinct")
        if weak and child_distinct is not None and child_distinct < MIN_VALUE_ONLY_DISTINCT:
            continue  # value-only evidence on a handful of values proves nothing
        add(_inclusion_query(rel.child, rel.child_columns[0], rel.parent, rel.parent_columns[0], dialect))

    # Value domains for low-cardinality categorical columns (never PII).
    for ds in snapshot.datasets:
        rows = ((profile.get("datasets") or {}).get(ds.name) or {}).get("row_count") or 0
        for col in ds.columns:
            if col.family not in _VALUE_FAMILIES or classify_name(col.name) != "none" or is_id_like(col.name):
                continue
            stats = profile_column(profile, ds.name, col.name)
            distinct = stats.get("distinct")
            if distinct is None or distinct == 0 or distinct > _MAX_DOMAIN or (rows and distinct > max(2, rows * 0.5)):
                continue
            add(_values_query(ds.name, col.name, dialect))

    truncated = len(queries) > budget
    return {"stage": 2, "dialect": dialect, "queries": queries[:budget], "truncated": truncated, "budget": budget}


def profile_column(profile: dict[str, Any], dataset: str, column: str) -> dict[str, Any]:
    return (((profile.get("datasets") or {}).get(dataset) or {}).get("columns") or {}).get(column) or {}


def _approx_unique(profile: dict[str, Any], dataset: str, column: str) -> bool:
    ds = (profile.get("datasets") or {}).get(dataset) or {}
    rows = ds.get("row_count")
    stats = (ds.get("columns") or {}).get(column) or {}
    if not rows or stats.get("distinct") is None:
        return bool(rows is None)
    return stats.get("non_null", 0) >= rows * 0.98 and stats["distinct"] >= rows * 0.97


def _uniqueness_query(dataset: str, columns: list[str], dialect: str) -> dict[str, Any]:
    cols = ", ".join(_q(c) for c in columns)
    not_null = " AND ".join(f"{_q(c)} IS NOT NULL" for c in columns)
    sql = (
        f"SELECT COUNT(*) AS duplicate_groups FROM (SELECT {cols} FROM {_table(dataset, dialect)} "
        f"WHERE {not_null} GROUP BY {cols} HAVING COUNT(*) > 1) d"
    )
    return {"kind": "unique", "dataset": dataset, "columns": list(columns), "sql": _finalize(sql, dialect)}


def _inclusion_query(child: str, ccol: str, parent: str, pcol: str, dialect: str) -> dict[str, Any]:
    c, p = _q(ccol), _q(pcol)
    ct, pt = _table(child, dialect), _table(parent, dialect)
    sql = (
        "SELECT "
        f"(SELECT COUNT(*) FROM {ct}) AS child_rows, "
        f"(SELECT COUNT({c}) FROM {ct}) AS child_non_null, "
        f"(SELECT COUNT(DISTINCT {c}) FROM {ct}) AS child_distinct, "
        f"(SELECT COUNT(*) FROM (SELECT DISTINCT {c} AS v FROM {ct} WHERE {c} IS NOT NULL) x "
        f"WHERE x.v IN (SELECT {p} FROM {pt})) AS matched_distinct"
    )
    return {
        "kind": "inclusion", "child": child, "child_columns": [ccol], "parent": parent, "parent_columns": [pcol],
        "sql": _finalize(sql, dialect),
    }


def _values_query(dataset: str, column: str, dialect: str) -> dict[str, Any]:
    c = _q(column)
    sql = (
        f"SELECT {c} AS value, COUNT(*) AS n FROM {_table(dataset, dialect)} "
        f"GROUP BY {c} ORDER BY n DESC, value LIMIT {_MAX_DOMAIN + 1}"
    )
    return {"kind": "values", "dataset": dataset, "column": column, "sql": _finalize(sql, dialect)}


# ---------------------------------------------------------------------- ingest
def empty_profile(dialect: str = "ansi") -> dict[str, Any]:
    return {"version": PROFILE_VERSION, "dialect": dialect, "datasets": {}, "inclusions": []}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value) if value != value.to_integral_value() else int(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return None
    return value


def _lower_keys(row: dict[str, Any]) -> dict[str, Any]:
    return {str(k).lower(): _jsonable(v) for k, v in row.items()}


def ingest(plan: dict[str, Any], results: dict[str, list[dict[str, Any]]], profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fold query results (``{query_id: [row, ...]}``) into a profile document."""
    profile = profile or empty_profile(plan.get("dialect", "ansi"))
    datasets = profile.setdefault("datasets", {})
    inclusions = profile.setdefault("inclusions", [])
    missing = []
    for query in plan.get("queries", []):
        rows = results.get(query["id"])
        if rows is None:
            missing.append(query["id"])
            continue
        rows = [_lower_keys(r) for r in rows]
        kind = query["kind"]
        if kind == "stats" and rows:
            row = rows[0]
            ds = datasets.setdefault(query["dataset"], {})
            ds["row_count"] = int(row.get("row_count") or 0)
            ds["approx"] = bool(query.get("approx"))
            ds["sampled"] = bool(query.get("sampled"))
            cols = ds.setdefault("columns", {})
            for alias, name in query["columns_map"].items():
                entry = cols.setdefault(name, {})
                entry["non_null"] = _int(row.get(f"{alias}_nn"))
                if f"{alias}_nd" in row:
                    entry["distinct"] = _int(row.get(f"{alias}_nd"))
                if f"{alias}_min" in row:
                    entry["min"] = row.get(f"{alias}_min")
                    entry["max"] = row.get(f"{alias}_max")
        elif kind == "unique" and rows:
            ds = datasets.setdefault(query["dataset"], {})
            dup = _int(rows[0].get("duplicate_groups")) or 0
            bucket = "unique_keys" if dup == 0 else "non_unique_keys"
            keys = ds.setdefault(bucket, [])
            if query["columns"] not in keys:
                keys.append(query["columns"])
        elif kind == "inclusion" and rows:
            row = rows[0]
            child_distinct = _int(row.get("child_distinct")) or 0
            matched = _int(row.get("matched_distinct")) or 0
            record = {
                "child": query["child"], "child_columns": query["child_columns"],
                "parent": query["parent"], "parent_columns": query["parent_columns"],
                "child_rows": _int(row.get("child_rows")), "child_non_null": _int(row.get("child_non_null")),
                "child_distinct": child_distinct, "matched_distinct": matched,
                "ratio": round(matched / child_distinct, 4) if child_distinct else None,
            }
            inclusions[:] = [i for i in inclusions if not _same_inclusion(i, record)]
            inclusions.append(record)
        elif kind == "values":
            ds = datasets.setdefault(query["dataset"], {})
            entry = ds.setdefault("columns", {}).setdefault(query["column"], {})
            if classify_name(query["column"]) != "none":
                continue  # defence in depth: never store PII values
            values = [[r.get("value"), _int(r.get("n"))] for r in rows]
            if classify_values([v for v, _ in values]) != "none":
                entry["pii_by_value"] = True  # value shapes look personal: keep the fact, never the values
                continue
            entry["values_complete"] = len(values) <= _MAX_DOMAIN
            entry["values"] = values[:_MAX_DOMAIN]
    if plan.get("stage") == 2 and not missing:
        profile["stage2"] = True
    if missing:
        profile.setdefault("missing_results", [])
        profile["missing_results"] = sorted(set(profile["missing_results"]) | set(missing))
    return profile


def _same_inclusion(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (a["child"], a["child_columns"], a["parent"], a["parent_columns"]) == (
        b["child"], b["child_columns"], b["parent"], b["parent_columns"])


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------- run
def _execute_plan(path: str | Path, plan: dict[str, Any], timeout_s: float) -> dict[str, list[dict[str, Any]]]:
    engine = detect_engine(path)
    results: dict[str, list[dict[str, Any]]] = {}
    for query in plan["queries"]:
        sql = query["sql"] if engine == "duckdb" else transpile(query["sql"], "sqlite", read=canonical_dialect(plan["dialect"]) or "duckdb")
        result = run_query(path, sql, timeout_s=timeout_s, engine=engine)
        results[query["id"]] = result.as_dicts()
    return results


def run(path: str | Path, snapshot: Snapshot, *, timeout_s: float = 60.0, value_fallback: bool = True) -> dict[str, Any]:
    """Profile a local SQLite/DuckDB database (both stages, read-only)."""
    engine = detect_engine(path)
    plan1 = emit_stage1(snapshot, engine)
    profile = ingest(plan1, _execute_plan(path, plan1, timeout_s), empty_profile(engine))
    plan2 = emit_stage2(snapshot, profile, engine, budget=10_000, value_fallback=value_fallback)
    profile = ingest(plan2, _execute_plan(path, plan2, timeout_s), profile)
    return profile


def save_profile(profile: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((json.dumps(profile, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8"))
    return target


def load_profile(path: str | Path) -> dict[str, Any]:
    file = Path(path)
    return json.loads(file.read_text(encoding="utf-8")) if file.is_file() else empty_profile()
