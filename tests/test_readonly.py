"""Read-only execution safety for local SQLite and DuckDB."""

from __future__ import annotations

import sqlite3

import pytest

from mautoontology.readonly import (
    QueryTimeout,
    ReadOnlyViolation,
    assert_single_select,
    run_query,
)


@pytest.fixture()
def sqlite_db(tmp_path):
    db = tmp_path / "app.sqlite"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT); INSERT INTO t VALUES (1, 'a'), (2, 'b');")
    con.close()
    return db


@pytest.mark.parametrize("sql", [
    "DELETE FROM t", "DROP TABLE t", "INSERT INTO t VALUES (3, 'c')", "UPDATE t SET v = 'x'",
    "SELECT 1; SELECT 2", "ATTACH DATABASE 'x.db' AS x", "PRAGMA writable_schema = 1", "CREATE TABLE x AS SELECT 1",
    "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
])
def test_non_select_statements_are_rejected(sql):
    with pytest.raises(ReadOnlyViolation):
        assert_single_select(sql, "sqlite")


def test_select_runs_and_caps_rows(sqlite_db):
    result = run_query(sqlite_db, "SELECT id, v FROM t ORDER BY id", max_rows=1)
    assert result.columns == ["id", "v"]
    assert result.rows == [(1, "a")] and result.truncated


def test_sqlite_authorizer_blocks_writes_even_without_the_parser_check(sqlite_db):
    with pytest.raises(sqlite3.DatabaseError):
        run_query(sqlite_db, "DELETE FROM t", check=False)
    assert run_query(sqlite_db, "SELECT COUNT(*) FROM t").rows == [(2,)]


def test_sqlite_timeout(sqlite_db):
    runaway = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT COUNT(*) FROM r"
    with pytest.raises(QueryTimeout):
        run_query(sqlite_db, runaway, timeout_s=0.3)


def test_duckdb_is_read_only_and_times_out(trap_db):
    assert run_query(trap_db, "SELECT COUNT(*) FROM orders").rows == [(3000,)]
    with pytest.raises(Exception):
        run_query(trap_db, "DELETE FROM orders", check=False)
    runaway = "SELECT COUNT(*) FROM range(100000000) a, range(100000) b"
    with pytest.raises(QueryTimeout):
        run_query(trap_db, runaway, timeout_s=0.5)


def test_missing_database_is_not_created(tmp_path):
    with pytest.raises(FileNotFoundError):
        run_query(tmp_path / "nope.sqlite", "SELECT 1", engine="sqlite")
    assert not (tmp_path / "nope.sqlite").exists()
