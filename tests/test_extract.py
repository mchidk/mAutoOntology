"""Extraction: DDL in many dialects, information_schema exports, dbt artifacts, local databases, merging."""

from __future__ import annotations

import json

import pytest

from mautoontology.extract import (
    extract_dbt,
    extract_ddl,
    extract_duckdb,
    extract_infoschema,
    extract_sqlite,
    infoschema_query,
)
from mautoontology.snapshot import Snapshot, merge_snapshots, type_family

from .conftest import FIXTURES

DIALECTS = ["snowflake", "databricks", "postgres", "tsql", "bigquery"]


def _signature(snapshot: Snapshot):
    return sorted(
        (ds.name, tuple(ds.primary_key), tuple((c.name, c.family, c.nullable) for c in ds.columns),
         tuple(sorted((tuple(f.columns), f.ref_dataset, tuple(f.ref_columns)) for f in ds.foreign_keys)))
        for ds in snapshot.datasets
    )


@pytest.mark.parametrize(
    ("raw", "dialect", "family"),
    [
        ("NUMBER(38,0)", None, "integer"), ("NUMBER(12,2)", None, "decimal"), ("INT64", None, "integer"),
        ("TIMESTAMP_TZ", None, "timestamp_tz"), ("TIMESTAMP_NTZ", None, "timestamp"), ("DATETIMEOFFSET", None, "timestamp_tz"),
        ("TIMESTAMP", "databricks", "timestamp_tz"), ("TIMESTAMP", "snowflake", "timestamp"), ("NVARCHAR(20)", None, "string"),
        ("VARIANT", None, "json"), ("BOOLEAN", None, "boolean"), ("double precision", None, "float"), ("", None, "other"),
    ],
)
def test_type_family(raw, dialect, family):
    assert type_family(raw, dialect) == family


def test_same_schema_in_five_dialects_gives_identical_snapshots():
    signatures = {}
    for dialect in DIALECTS:
        snapshot, warnings = extract_ddl([FIXTURES / "ddl_dialects" / f"{dialect}.sql"], dialect)
        assert warnings == [], (dialect, warnings)
        signatures[dialect] = _signature(snapshot)
    first = signatures["snowflake"]
    assert len(first) == 4
    for dialect, signature in signatures.items():
        assert signature == first, f"{dialect} differs from snowflake"


def test_ddl_captures_keys_foreign_keys_and_comments():
    snapshot, _ = extract_ddl([FIXTURES / "ddl_dialects" / "postgres.sql"], "postgres")
    items = snapshot.get("shop.order_items")
    assert items.primary_key == ["order_id", "line_no"]
    assert {fk.ref_dataset for fk in items.foreign_keys} == {"shop.orders", "shop.products"}
    assert snapshot.get("shop.customers").description == "One row per customer"  # COMMENT ON TABLE


def test_unparseable_statements_are_warnings_not_failures(tmp_path):
    ddl = tmp_path / "x.sql"
    ddl.write_text("CREATE TABLE t (id INT PRIMARY KEY);\nGRANT SELECT ON t TO r;\n", encoding="utf-8")
    snapshot, warnings = extract_ddl([ddl], "postgres")
    assert snapshot.get("t").primary_key == ["id"]


def test_views_record_columns_and_lineage(tmp_path):
    ddl = tmp_path / "v.sql"
    ddl.write_text(
        "CREATE TABLE a (id INT, x INT); CREATE TABLE b (id INT);\n"
        "CREATE VIEW v AS WITH c AS (SELECT id FROM b) SELECT a.id, a.x AS amount FROM a JOIN c ON a.id = c.id;",
        encoding="utf-8",
    )
    snapshot, _ = extract_ddl([ddl], "duckdb")
    view = snapshot.get("v")
    assert view.kind == "view"
    assert [c.name for c in view.columns] == ["id", "amount"]
    assert view.depends_on == ["a", "b"]


def test_infoschema_csv_and_snowflake_show_keys(tmp_path):
    columns = tmp_path / "columns.csv"
    columns.write_text(
        "TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,DATA_TYPE,IS_NULLABLE,COMMENT,ORDINAL_POSITION\n"
        "SALES,ORDERS,ORDER_ID,NUMBER,NO,,1\nSALES,ORDERS,CUSTOMER_ID,NUMBER,YES,,2\n"
        "SALES,CUSTOMERS,CUSTOMER_ID,NUMBER,NO,Customer key,1\n",
        encoding="utf-8",
    )
    pks = tmp_path / "pks.json"
    pks.write_text(json.dumps([{"schema_name": "SALES", "table_name": "CUSTOMERS", "column_name": "CUSTOMER_ID",
                                "key_sequence": 1, "constraint_name": "PK_C"}]), encoding="utf-8")
    fks = tmp_path / "fks.json"
    fks.write_text(json.dumps([{"pk_schema_name": "SALES", "pk_table_name": "CUSTOMERS", "pk_column_name": "CUSTOMER_ID",
                                "fk_schema_name": "SALES", "fk_table_name": "ORDERS", "fk_column_name": "CUSTOMER_ID",
                                "key_sequence": 1, "fk_name": "FK_O_C"}]), encoding="utf-8")
    snapshot, warnings = extract_infoschema([columns, pks, fks], "snowflake")
    assert warnings == []
    orders, customers = snapshot.get("sales.orders"), snapshot.get("sales.customers")
    assert customers.primary_key == ["customer_id"]
    assert customers.column("customer_id").description == "Customer key"
    assert orders.foreign_keys[0].ref_dataset == "sales.customers"
    assert orders.column("order_id").nullable is False


def test_infoschema_query_emits_dialect_specific_sql():
    assert "SHOW IMPORTED KEYS" in infoschema_query("snowflake", "ANALYTICS")["keys"]
    assert "full_data_type" in infoschema_query("databricks")["columns"]
    assert "UPPER('x''y')" in infoschema_query("postgres", "x'y")["columns"]  # quotes are escaped


def test_dbt_manifest_and_catalog():
    snapshot, warnings = extract_dbt(FIXTURES / "jaffle_dbt")
    assert warnings == []
    assert snapshot.dialect == "snowflake"
    orders = snapshot.get("analytics.jaffle.orders")
    assert orders.row_count == 61948
    assert orders.column("order_total").family == "decimal"  # type from catalog.json
    assert orders.foreign_keys[0].origin == "dbt_test"
    assert orders.accepted_values["status"] == ["placed", "shipped", "completed", "returned"]
    assert orders.depends_on == ["analytics.jaffle.customers"]
    assert snapshot.get("analytics.jaffle.customers").unique_keys == [["customer_id"]]
    semantic = snapshot.semantic["dbt"]
    assert semantic["semantic_models"][0]["dataset"] == "analytics.jaffle.orders"
    assert {m["name"] for m in semantic["metrics"]} == {"revenue", "orders", "average_order_value"}


def test_dbt_schema_yml_fallback():
    snapshot, warnings = extract_dbt(FIXTURES / "jaffle_dbt_yml")
    assert any("falling back" in w for w in warnings)
    orders = snapshot.get("orders")
    assert orders.foreign_keys[0].ref_dataset == "customers"
    assert orders.unique_keys == [["order_id"]]
    assert "status" in orders.accepted_values


def test_local_duckdb_and_sqlite(tmp_path, trap_db):
    snapshot = extract_duckdb(trap_db)
    assert len(snapshot.datasets) == 13
    assert snapshot.get("orders").row_count == 3000

    import sqlite3

    db = tmp_path / "app.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE parent (id INTEGER PRIMARY KEY, code TEXT UNIQUE);"
        "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent);"
        "INSERT INTO parent VALUES (1, 'a');"
    )
    con.close()
    lite = extract_sqlite(db)
    child = lite.get("child")
    assert child.foreign_keys[0].ref_dataset == "parent"
    assert child.foreign_keys[0].ref_columns == ["id"]  # implicit reference resolved to the parent key
    assert lite.get("parent").unique_keys == [["code"]]


def test_merge_combines_dbt_semantics_with_warehouse_types(tmp_path):
    dbt_snapshot, _ = extract_dbt(FIXTURES / "jaffle_dbt_yml")
    columns = tmp_path / "c.csv"
    columns.write_text("table_schema,table_name,column_name,data_type\nanalytics,orders,order_id,BIGINT\n", encoding="utf-8")
    info, _ = extract_infoschema([columns], "postgres")
    merged = merge_snapshots([dbt_snapshot, info])
    orders = merged.get("orders")
    assert orders.name == "analytics.orders"  # more qualified name wins
    assert orders.column("order_id").family == "integer"
    assert orders.foreign_keys  # dbt test survived the merge


def test_snapshot_round_trip(tmp_path, trap_db):
    snapshot = extract_duckdb(trap_db)
    path = snapshot.save(tmp_path / "snap.json")
    assert Snapshot.load(path).to_dict() == snapshot.to_dict()
