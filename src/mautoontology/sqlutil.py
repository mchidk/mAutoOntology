"""Dialect-aware SQL helpers (identifier quoting and transpilation)."""

from __future__ import annotations

import re

import sqlglot
from sqlglot.dialects.dialect import Dialect, NormalizationStrategy

_SAFE = re.compile(r"^[a-z_][a-z0-9_]*$")
# Words that must be quoted when used as identifiers in at least one major dialect.
_RESERVED = {
    "all", "and", "any", "as", "between", "by", "case", "cast", "check", "column", "constraint", "create",
    "cross", "current", "current_date", "current_time", "current_timestamp", "current_user", "date", "default",
    "delete", "desc", "distinct", "drop", "else", "end", "exists", "false", "for", "from", "full", "grant",
    "group", "having", "in", "inner", "insert", "intersect", "interval", "into", "is", "join", "key", "left",
    "like", "limit", "natural", "not", "null", "of", "on", "or", "order", "outer", "primary", "qualify",
    "references", "right", "row", "rows", "select", "set", "some", "table", "then", "time", "timestamp", "to",
    "true", "union", "unique", "update", "user", "using", "value", "values", "when", "where", "window", "with",
}


def canonical_dialect(dialect: str | None) -> str:
    name = (dialect or "").lower()
    aliases = {"ansi": "", "sqlserver": "tsql", "mssql": "tsql", "fabric": "tsql", "synapse": "tsql", "spark_sql": "spark"}
    return aliases.get(name, name)


def quote_ident(name: str, dialect: str | None) -> str:
    """Render an identifier so it resolves to the stored (normalized) name."""
    dialect = canonical_dialect(dialect)
    if _SAFE.match(name) and name not in _RESERVED:
        return name
    dialect_obj = Dialect.get_or_raise(dialect or None)
    stored = name.upper() if dialect_obj.normalization_strategy == NormalizationStrategy.UPPERCASE else name
    open_q, close_q = dialect_obj.IDENTIFIER_START, dialect_obj.IDENTIFIER_END
    escaped = stored.replace(close_q, close_q + close_q)  # a hostile name can never close the quote
    return f"{open_q}{escaped}{close_q}"


def entity_alias(entity_id: str) -> str:
    """A SQL-safe table alias for an entity id (``order`` is reserved, so it becomes ``order_``)."""
    alias = re.sub(r"[^a-z0-9_]", "_", entity_id.lower())
    if not _SAFE.match(alias) or alias in _RESERVED:
        alias = f"{alias}_"
    return alias


def qualified(dataset: str, dialect: str | None) -> str:
    return ".".join(quote_ident(part, dialect) for part in dataset.split("."))


def transpile(sql: str, dialect: str | None, read: str = "duckdb") -> str:
    """Transpile portable SQL (written in DuckDB syntax) to ``dialect``."""
    target = canonical_dialect(dialect)
    if not target or target == read:
        return sql
    return ";\n".join(sqlglot.transpile(sql, read=read, write=target))
