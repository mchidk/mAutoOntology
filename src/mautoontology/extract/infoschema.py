"""Extract a snapshot from information_schema exports (CSV or JSON).

This is the "bring your own executor" path for live warehouses: the agent runs
:func:`infoschema_query` through whatever access it already has (Snowflake,
Databricks, Postgres, SQL Server, BigQuery, DuckDB, ...), saves the rows as CSV
or JSON, and this module turns them into a snapshot. No credentials ever pass
through mAutoOntology.

Column rows need: table_schema, table_name, column_name, data_type
(optional: table_catalog, is_nullable, comment/column_comment, ordinal_position).
Key rows (optional) need: constraint_type (PRIMARY KEY | UNIQUE | FOREIGN KEY),
table_schema, table_name, column_name, ordinal_position and, for foreign keys,
ref_table_schema, ref_table_name, ref_column_name. Snowflake ``SHOW PRIMARY KEYS``
and ``SHOW IMPORTED KEYS`` output is also accepted as-is.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..snapshot import Column, Dataset, ForeignKey, Snapshot, norm_dataset, norm_ident

_COLUMNS_SQL = {
    "default": """SELECT table_catalog, table_schema, table_name, column_name, data_type, is_nullable, ordinal_position
FROM information_schema.columns
WHERE table_schema NOT IN ('information_schema', 'pg_catalog', 'INFORMATION_SCHEMA'){schema_filter}
ORDER BY table_schema, table_name, ordinal_position""",
    "snowflake": """SELECT table_catalog, table_schema, table_name, column_name, data_type, is_nullable, comment, ordinal_position
FROM information_schema.columns
WHERE table_schema <> 'INFORMATION_SCHEMA'{schema_filter}
ORDER BY table_schema, table_name, ordinal_position""",
    "databricks": """SELECT table_catalog, table_schema, table_name, column_name, full_data_type AS data_type, is_nullable, comment, ordinal_position
FROM information_schema.columns
WHERE table_schema <> 'information_schema'{schema_filter}
ORDER BY table_schema, table_name, ordinal_position""",
    "bigquery": """SELECT table_catalog, table_schema, table_name, column_name, data_type, is_nullable, ordinal_position
FROM `region-us`.INFORMATION_SCHEMA.COLUMNS
WHERE TRUE{schema_filter}
ORDER BY table_schema, table_name, ordinal_position""",
}

_KEYS_SQL_STANDARD = """SELECT tc.constraint_type, kcu.table_schema, kcu.table_name, kcu.column_name, kcu.ordinal_position,
       ccu.table_schema AS ref_table_schema, ccu.table_name AS ref_table_name, ccu.column_name AS ref_column_name,
       tc.constraint_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
LEFT JOIN information_schema.referential_constraints rc
  ON tc.constraint_name = rc.constraint_name AND tc.table_schema = rc.constraint_schema
LEFT JOIN information_schema.key_column_usage ccu
  ON rc.unique_constraint_name = ccu.constraint_name AND rc.unique_constraint_schema = ccu.table_schema
 AND ccu.ordinal_position = kcu.position_in_unique_constraint
WHERE tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE', 'FOREIGN KEY'){schema_filter}"""

_KEYS_SQL = {
    "postgres": _KEYS_SQL_STANDARD,
    "duckdb": _KEYS_SQL_STANDARD,
    "tsql": _KEYS_SQL_STANDARD,
    "mysql": _KEYS_SQL_STANDARD,
    "databricks": _KEYS_SQL_STANDARD,
    "snowflake": "SHOW PRIMARY KEYS IN SCHEMA {schema};\nSHOW IMPORTED KEYS IN SCHEMA {schema};\nSHOW UNIQUE KEYS IN SCHEMA {schema};",
}


def infoschema_query(dialect: str, schema: str | None = None) -> dict[str, str]:
    """Return the read-only catalog queries to run for ``dialect``.

    The result maps ``columns`` (always) and ``keys`` (when the warehouse exposes
    key metadata) to SQL text. Save each result set as CSV or JSON and pass the
    files to ``mautoontology extract --infoschema``.
    """
    dialect = dialect.lower()
    schema_filter = ""
    if schema:
        safe = schema.replace("'", "''")
        schema_filter = f"\n  AND UPPER(table_schema) = UPPER('{safe}')"
    columns = _COLUMNS_SQL.get(dialect, _COLUMNS_SQL["default"]).format(schema_filter=schema_filter)
    out = {"columns": columns}
    keys = _KEYS_SQL.get(dialect)
    if keys:
        if dialect == "snowflake":
            out["keys"] = keys.format(schema=schema or "<database>.<schema>")
        else:
            out["keys"] = keys.format(schema_filter=schema_filter.replace("table_schema", "kcu.table_schema"))
    return out


def _read_rows(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("rows") or data.get("data") or []
        rows = list(data)
    else:
        rows = list(csv.DictReader(text.splitlines()))
    return [{str(k).strip().lower(): v for k, v in row.items()} for row in rows]


def _get(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def _truthy_nullable(value: Any) -> bool:
    if value is None:
        return True
    return str(value).strip().upper() not in {"NO", "N", "FALSE", "0"}


def _qualified(schema: Any, table: Any, catalog: Any = None) -> str:
    parts = [str(p) for p in (catalog, schema, table) if p not in (None, "")]
    return norm_dataset(".".join(parts))


def extract_infoschema(paths: Iterable[str | Path], dialect: str = "ansi") -> tuple[Snapshot, list[str]]:
    """Build a snapshot from information_schema / SHOW output files."""
    snapshot = Snapshot(dialect=dialect)
    warnings: list[str] = []
    column_rows: list[dict[str, Any]] = []
    key_rows: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw)
        rows = _read_rows(path)
        snapshot.sources.append(f"infoschema:{path.as_posix()}")
        if not rows:
            warnings.append(f"{path.as_posix()}: no rows")
            continue
        sample = rows[0]
        if "column_name" in sample and "data_type" in sample:
            column_rows.extend(rows)
        elif "constraint_type" in sample or "fk_table_name" in sample or "key_sequence" in sample:
            key_rows.extend(rows)
        else:
            warnings.append(f"{path.as_posix()}: unrecognized columns {sorted(sample)[:8]}")

    datasets: dict[str, Dataset] = {}
    column_rows.sort(key=lambda r: (str(_get(r, "table_schema")), str(_get(r, "table_name")), int(_get(r, "ordinal_position") or 0)))
    for row in column_rows:
        name = _qualified(_get(row, "table_schema"), _get(row, "table_name"))
        dataset = datasets.setdefault(name, Dataset(name=name))
        dataset.columns.append(
            Column(
                name=str(_get(row, "column_name")),
                data_type=str(_get(row, "data_type") or ""),
                nullable=_truthy_nullable(_get(row, "is_nullable")),
                description=_get(row, "comment", "column_comment", "description"),
            )
        )
        if not _truthy_nullable(_get(row, "is_nullable")):
            dataset.not_null.append(norm_ident(str(_get(row, "column_name"))))

    _apply_keys(datasets, key_rows, warnings)
    for dataset in datasets.values():
        snapshot.add(dataset)
    return snapshot, warnings


def _apply_keys(datasets: dict[str, Dataset], rows: list[dict[str, Any]], warnings: list[str]) -> None:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if "fk_table_name" in row:  # Snowflake SHOW IMPORTED KEYS
            ctype = "FOREIGN KEY"
            table = _qualified(_get(row, "fk_schema_name"), _get(row, "fk_table_name"))
            cname = str(_get(row, "fk_name") or f"fk_{table}")
        elif "constraint_type" in row:
            ctype = str(row["constraint_type"]).upper()
            table = _qualified(_get(row, "table_schema"), _get(row, "table_name"))
            cname = str(_get(row, "constraint_name") or f"{ctype}_{table}")
        else:  # Snowflake SHOW PRIMARY KEYS / SHOW UNIQUE KEYS
            ctype = "UNIQUE" if "unique" in str(_get(row, "constraint_name") or "").lower() else "PRIMARY KEY"
            table = _qualified(_get(row, "schema_name"), _get(row, "table_name"))
            cname = str(_get(row, "constraint_name") or f"pk_{table}")
        grouped[(table, ctype, cname)].append(row)

    for (table, ctype, _), members in grouped.items():
        dataset = datasets.get(table)
        if dataset is None:
            warnings.append(f"key on unknown table {table}")
            continue
        members.sort(key=lambda r: int(_get(r, "ordinal_position", "key_sequence") or 0))
        if ctype == "FOREIGN KEY":
            cols = [str(_get(r, "column_name", "fk_column_name")) for r in members]
            ref_cols = [str(_get(r, "ref_column_name", "pk_column_name")) for r in members]
            ref_table = _qualified(
                _get(members[0], "ref_table_schema", "pk_schema_name"),
                _get(members[0], "ref_table_name", "pk_table_name"),
            )
            dataset.foreign_keys.append(ForeignKey(columns=cols, ref_dataset=ref_table, ref_columns=ref_cols, origin="catalog"))
        else:
            cols = [norm_ident(str(_get(r, "column_name"))) for r in members]
            if ctype == "PRIMARY KEY" and not dataset.primary_key:
                dataset.primary_key = cols
            elif cols not in dataset.unique_keys:
                dataset.unique_keys.append(cols)
