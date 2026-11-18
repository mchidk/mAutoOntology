"""Extract snapshots directly from local SQLite and DuckDB files (read-only)."""

from __future__ import annotations

from pathlib import Path

from ..readonly import open_duckdb, open_sqlite
from ..snapshot import Column, Dataset, ForeignKey, Snapshot


def extract_sqlite(path: str | Path) -> Snapshot:
    con = open_sqlite(path)
    try:
        snapshot = Snapshot(dialect="sqlite", sources=[f"sqlite:{Path(path).as_posix()}"])
        tables = con.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for name, kind in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            dataset = Dataset(name=name, kind=kind)
            pk_cols: list[tuple[int, str]] = []
            for _, col, ctype, notnull, _, pk in con.execute(f"PRAGMA table_info({quoted})").fetchall():
                dataset.columns.append(Column(name=col, data_type=ctype or "", nullable=not notnull and not pk))
                if notnull:
                    dataset.not_null.append(col.lower())
                if pk:
                    pk_cols.append((pk, col))
            dataset.primary_key = [c for _, c in sorted(pk_cols)]
            fks: dict[int, ForeignKey] = {}
            for fk_id, _seq, ref_table, from_col, to_col, *_ in con.execute(f"PRAGMA foreign_key_list({quoted})").fetchall():
                fk = fks.setdefault(fk_id, ForeignKey(columns=[], ref_dataset=ref_table, ref_columns=[], origin="ddl"))
                fk.columns.append(from_col.lower())
                fk.ref_columns.append((to_col or "").lower())
            dataset.foreign_keys = list(fks.values())
            for _, idx_name, unique, *_ in con.execute(f"PRAGMA index_list({quoted})").fetchall():
                if unique:
                    quoted_index = '"' + idx_name.replace('"', '""') + '"'
                    cols = [r[2].lower() for r in con.execute(f"PRAGMA index_info({quoted_index})").fetchall()]
                    if cols and cols != dataset.primary_key and cols not in dataset.unique_keys:
                        dataset.unique_keys.append(cols)
            if kind == "table":
                dataset.row_count = con.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            snapshot.add(dataset)
        # Resolve FKs that reference the parent's primary key implicitly.
        for dataset in snapshot.datasets:
            for fk in dataset.foreign_keys:
                if not any(fk.ref_columns):
                    parent = snapshot.get(fk.ref_dataset)
                    if parent is not None:
                        fk.ref_columns = list(parent.primary_key)
        return snapshot
    finally:
        con.close()


def extract_duckdb(path: str | Path) -> Snapshot:
    con = open_duckdb(path)
    try:
        snapshot = Snapshot(dialect="duckdb", sources=[f"duckdb:{Path(path).as_posix()}"])
        rows = con.execute(
            """SELECT c.table_schema, c.table_name, c.column_name, c.data_type, c.is_nullable, t.table_type
               FROM information_schema.columns c
               JOIN information_schema.tables t USING (table_catalog, table_schema, table_name)
               WHERE c.table_schema NOT IN ('information_schema', 'pg_catalog')
               ORDER BY c.table_schema, c.table_name, c.ordinal_position"""
        ).fetchall()
        datasets: dict[str, Dataset] = {}
        for schema, table, col, ctype, nullable, ttype in rows:
            name = f"{schema}.{table}" if schema != "main" else table
            dataset = datasets.get(name)
            if dataset is None:
                dataset = datasets[name] = Dataset(name=name, kind="view" if "VIEW" in ttype else "table")
            dataset.columns.append(Column(name=col, data_type=ctype, nullable=nullable == "YES"))
            if nullable != "YES":
                dataset.not_null.append(col.lower())
        for schema, table, ctype, cols, ref_table, ref_cols in con.execute(
            """SELECT schema_name, table_name, constraint_type, constraint_column_names,
                      referenced_table, referenced_column_names
               FROM duckdb_constraints()
               WHERE constraint_type IN ('PRIMARY KEY', 'UNIQUE', 'FOREIGN KEY')"""
        ).fetchall():
            name = f"{schema}.{table}" if schema != "main" else table
            dataset = datasets.get(name.lower()) or datasets.get(name)
            if dataset is None:
                continue
            cols = [c.lower() for c in cols]
            if ctype == "PRIMARY KEY":
                dataset.primary_key = cols
            elif ctype == "UNIQUE":
                if cols not in dataset.unique_keys:
                    dataset.unique_keys.append(cols)
            else:
                ref = f"{schema}.{ref_table}" if schema != "main" else ref_table
                dataset.foreign_keys.append(ForeignKey(columns=cols, ref_dataset=ref, ref_columns=list(ref_cols or []), origin="ddl"))
        for name, dataset in datasets.items():
            if dataset.kind == "table":
                quoted = ".".join('"' + p.replace('"', '""') + '"' for p in name.split("."))
                dataset.row_count = con.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            snapshot.add(dataset)
        return snapshot
    finally:
        con.close()
