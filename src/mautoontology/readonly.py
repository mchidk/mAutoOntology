"""Read-only, time-bounded query execution for local SQLite and DuckDB files.

This is the only place mAutoOntology executes SQL itself. Remote warehouses are
never contacted: for them the toolkit emits SQL and the agent runs it with its
own access. Safety layers here:

* SQLite: opened with ``mode=ro`` (the file must exist; nothing is created),
  plus an authorizer that denies every non-read action (writes, ATTACH, PRAGMA
  writes, extension loading, transactions), plus a progress-handler timeout.
* DuckDB: opened with ``read_only=True`` and external access disabled (no file
  or network reads, no extension loading, configuration locked); a watchdog
  thread interrupts queries that exceed the timeout.
* Both: only a single SELECT/WITH statement is accepted (checked with sqlglot
  before execution) and results are capped at ``max_rows``.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_ROWS = 10_000

_SQLITE_READ_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE if hasattr(sqlite3, "SQLITE_RECURSIVE") else sqlite3.SQLITE_SELECT,
}
# PRAGMAs that only read schema metadata (used by the extractor).
_SQLITE_READ_PRAGMAS = {"table_info", "table_xinfo", "foreign_key_list", "index_list", "index_info"}


class ReadOnlyViolation(ValueError):
    """Raised when a statement is not a single read-only query."""


class QueryTimeout(TimeoutError):
    """Raised when a query exceeds its time budget."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool = False
    elapsed_s: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row)) for row in self.rows]


_WRITE_NODES = tuple(
    getattr(exp, name)
    for name in (
        "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "Command", "TruncateTable",
        "Grant", "Revoke", "Copy", "Pragma", "Set", "Use", "Transaction", "Commit", "Rollback", "Attach",
        "Detach", "Install", "LoadData", "Into",
    )
    if hasattr(exp, name)
)


def assert_single_select(sql: str, dialect: str) -> exp.Expression:
    """Parse ``sql`` and ensure it is exactly one read-only query."""
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except sqlglot.errors.ParseError as error:
        raise ReadOnlyViolation(f"SQL does not parse in dialect '{dialect}': {error}") from error
    if len(statements) != 1:
        raise ReadOnlyViolation(f"expected exactly one statement, got {len(statements)}")
    statement = statements[0]
    if not isinstance(statement, (exp.Query, exp.Subquery)):
        raise ReadOnlyViolation(f"only SELECT/WITH queries are allowed (got {type(statement).__name__})")
    for node in statement.walk():
        if isinstance(node, _WRITE_NODES):
            raise ReadOnlyViolation(f"write or session statement not allowed: {type(node).__name__}")
    return statement


def _sqlite_authorizer(action: int, arg1: Any, arg2: Any, _db: Any, _trigger: Any) -> int:
    if action in _SQLITE_READ_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and str(arg1).lower() in _SQLITE_READ_PRAGMAS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def open_sqlite(path: str | Path) -> sqlite3.Connection:
    file = Path(path)
    if not file.is_file():
        raise FileNotFoundError(f"SQLite database not found: {file}")
    con = sqlite3.connect(f"{file.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
    con.set_authorizer(_sqlite_authorizer)
    return con


def open_duckdb(path: str | Path) -> Any:
    try:
        import duckdb
    except ImportError as error:  # pragma: no cover - optional extra
        raise RuntimeError("DuckDB support needs the optional extra: pip install 'mautoontology[duckdb]'") from error
    file = Path(path)
    if not file.is_file():
        raise FileNotFoundError(f"DuckDB database not found: {file}")
    # read_only protects the file; the config blocks file/network access (read_csv, glob, httpfs), extension
    # loading and SET statements, even from views stored inside the database.
    return duckdb.connect(str(file), read_only=True, config={
        "enable_external_access": False,
        "autoinstall_known_extensions": False,
        "autoload_known_extensions": False,
        "lock_configuration": True,
    })


def detect_engine(path: str | Path) -> str:
    """Return ``sqlite`` or ``duckdb`` from the file header (extension as fallback)."""
    file = Path(path)
    with file.open("rb") as handle:
        header = handle.read(16)
    if header.startswith(b"SQLite format 3"):
        return "sqlite"
    if file.suffix.lower() in {".duckdb", ".ddb"} or b"DUCK" in header:
        return "duckdb"
    return "duckdb"


def run_query(
    path: str | Path,
    sql: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_rows: int = DEFAULT_MAX_ROWS,
    engine: str | None = None,
    check: bool = True,
) -> QueryResult:
    """Run one read-only query against a local SQLite/DuckDB file."""
    engine = engine or detect_engine(path)
    if check:
        assert_single_select(sql, engine)
    started = time.monotonic()
    if engine == "sqlite":
        con = open_sqlite(path)
        deadline = started + timeout_s
        timed_out = {"hit": False}

        def _progress() -> int:
            if time.monotonic() > deadline:
                timed_out["hit"] = True
                return 1
            return 0

        con.set_progress_handler(_progress, 1000)
        try:
            cursor = con.execute(sql)
            columns = [d[0] for d in cursor.description or []]
            rows = cursor.fetchmany(max_rows + 1)
        except sqlite3.OperationalError as error:
            if timed_out["hit"]:
                raise QueryTimeout(f"query exceeded {timeout_s}s") from error
            raise
        finally:
            con.close()
    else:
        con = open_duckdb(path)
        timer = threading.Timer(timeout_s, con.interrupt)
        timer.start()
        try:
            cursor = con.execute(sql)
            columns = [d[0] for d in cursor.description or []]
            rows = cursor.fetchmany(max_rows + 1)
        except Exception as error:
            if time.monotonic() - started >= timeout_s:
                raise QueryTimeout(f"query exceeded {timeout_s}s") from error
            raise
        finally:
            timer.cancel()
            con.close()
    truncated = len(rows) > max_rows
    return QueryResult(
        columns=columns,
        rows=[tuple(r) for r in rows[:max_rows]],
        truncated=truncated,
        elapsed_s=round(time.monotonic() - started, 4),
    )
