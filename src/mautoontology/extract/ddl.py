"""Extract a snapshot from SQL DDL files in any dialect sqlglot understands.

Handles ``CREATE TABLE`` (column and table constraints, comments),
``CREATE VIEW`` (columns from the select list, lineage from referenced tables)
and ``ALTER TABLE ... ADD [CONSTRAINT] PRIMARY KEY / FOREIGN KEY / UNIQUE``.
Statements sqlglot cannot parse are reported as warnings, never fatal.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from pathlib import Path

import sqlglot
from sqlglot import exp

from ..snapshot import Column, Dataset, ForeignKey, Snapshot, norm_dataset, norm_ident


def _table_name(table: exp.Table) -> str:
    parts = [p for p in (table.catalog, table.db, table.name) if p]
    return norm_dataset(".".join(parts))


def _identifiers(node: exp.Expression | None) -> list[str]:
    if node is None:
        return []
    items = node.expressions if hasattr(node, "expressions") and node.expressions else [node]
    out = []
    for item in items:
        name = item.name if hasattr(item, "name") and item.name else item.sql()
        out.append(norm_ident(name))
    return out


def _comment(node: exp.Expression) -> str | None:
    for constraint in node.args.get("constraints") or []:
        kind = constraint.args.get("kind")
        if isinstance(kind, exp.CommentColumnConstraint):
            return kind.this.name if isinstance(kind.this, exp.Literal) else kind.this.sql()
    return None


def _apply_table_constraint(dataset: Dataset, node: exp.Expression, dialect: str) -> None:
    """Apply a table-level constraint (PRIMARY KEY / UNIQUE / FOREIGN KEY)."""
    target = node
    if isinstance(node, exp.Constraint):
        for inner in node.expressions:
            _apply_table_constraint(dataset, inner, dialect)
        return
    if isinstance(target, exp.PrimaryKey):
        dataset.primary_key = _identifiers(target)
    elif isinstance(target, exp.UniqueColumnConstraint) or type(target).__name__ == "Unique":
        cols = _identifiers(target.this) if target.this is not None else _identifiers(target)
        if cols:
            dataset.unique_keys.append(cols)
    elif isinstance(target, exp.ForeignKey):
        reference = target.args.get("reference")
        if reference is None:
            return
        ref_schema = reference.this
        ref_table = ref_schema.this if isinstance(ref_schema, exp.Schema) else ref_schema
        ref_cols = _identifiers(ref_schema) if isinstance(ref_schema, exp.Schema) else []
        dataset.foreign_keys.append(
            ForeignKey(
                columns=_identifiers(target),
                ref_dataset=_table_name(ref_table) if isinstance(ref_table, exp.Table) else ref_table.sql(),
                ref_columns=ref_cols,
                origin="ddl",
            )
        )


def _column_from_def(dataset: Dataset, coldef: exp.ColumnDef, dialect: str) -> None:
    kind = coldef.args.get("kind")
    data_type = kind.sql(dialect=dialect) if kind is not None else ""
    column = Column(name=coldef.name, data_type=data_type, description=_comment(coldef))
    for constraint in coldef.constraints:
        ckind = constraint.args.get("kind")
        if isinstance(ckind, exp.NotNullColumnConstraint) and not ckind.args.get("allow_null"):
            column.nullable = False
            dataset.not_null.append(column.name)
        elif isinstance(ckind, exp.PrimaryKeyColumnConstraint):
            dataset.primary_key = [column.name]
            column.nullable = False
        elif isinstance(ckind, exp.UniqueColumnConstraint):
            dataset.unique_keys.append([column.name])
        elif isinstance(ckind, exp.Reference):
            ref_schema = ckind.this
            ref_table = ref_schema.this if isinstance(ref_schema, exp.Schema) else ref_schema
            ref_cols = _identifiers(ref_schema) if isinstance(ref_schema, exp.Schema) else []
            dataset.foreign_keys.append(
                ForeignKey(
                    columns=[column.name],
                    ref_dataset=_table_name(ref_table) if isinstance(ref_table, exp.Table) else ref_table.sql(),
                    ref_columns=ref_cols,
                    origin="ddl",
                )
            )
    dataset.columns.append(column)


def _table_comment(create: exp.Create) -> str | None:
    props = create.args.get("properties")
    if props is None:
        return None
    for prop in props.expressions:
        if isinstance(prop, exp.SchemaCommentProperty):
            value = prop.this
            return value.name if isinstance(value, exp.Literal) else value.sql()
    return None


_GO_RE = re.compile(r"^\s*GO\s*;?\s*$", re.IGNORECASE | re.MULTILINE)


def parse_ddl(sql: str, dialect: str, snapshot: Snapshot, warnings: list[str], source: str = "") -> None:
    # T-SQL batch separators are a client convention, not SQL.
    sql = _GO_RE.sub(";", sql)
    logger = logging.getLogger("sqlglot")
    previous = logger.level
    logger.setLevel(logging.ERROR)
    try:
        statements = sqlglot.parse(sql, read=dialect, error_level=sqlglot.ErrorLevel.IGNORE)
    except sqlglot.errors.SqlglotError as error:
        warnings.append(f"{source}: could not parse DDL ({error})")
        return
    finally:
        logger.setLevel(previous)
    for statement in statements:
        if statement is None:
            continue
        if isinstance(statement, exp.Create):
            _handle_create(statement, dialect, snapshot, warnings, source)
        elif isinstance(statement, exp.Alter):
            _handle_alter(statement, dialect, snapshot, warnings, source)
        elif isinstance(statement, exp.Comment):
            _handle_comment(statement, snapshot)
        elif isinstance(statement, exp.Command):
            warnings.append(f"{source}: skipped unsupported statement: {statement.sql()[:80]}")
    for dataset in snapshot.datasets:
        for column in dataset.columns:
            column.refine_family(dialect)


def _handle_comment(statement: exp.Comment, snapshot: Snapshot) -> None:
    """``COMMENT ON TABLE|VIEW|COLUMN x IS '...'`` (Postgres, Snowflake, Oracle)."""
    kind = str(statement.args.get("kind") or "").upper()
    text_node = statement.expression
    text = text_node.name if isinstance(text_node, exp.Literal) else (text_node.sql() if text_node else None)
    target = statement.this
    if kind in {"TABLE", "VIEW"} and isinstance(target, exp.Table):
        dataset = snapshot.get(_table_name(target))
        if dataset is not None:
            dataset.description = text
    elif kind == "COLUMN" and isinstance(target, exp.Column):
        parts = [p for p in (target.args.get("catalog"), target.args.get("db"), target.args.get("table")) if p]
        dataset = snapshot.get(".".join(p.name for p in parts)) if parts else None
        column = dataset.column(target.name) if dataset else None
        if column is not None:
            column.description = text


def _handle_create(create: exp.Create, dialect: str, snapshot: Snapshot, warnings: list[str], source: str) -> None:
    kind = (create.args.get("kind") or "").upper()
    target = create.this
    if kind == "TABLE":
        table = target.this if isinstance(target, exp.Schema) else target
        if not isinstance(table, exp.Table):
            return
        dataset = Dataset(name=_table_name(table), kind="table", description=_table_comment(create))
        if isinstance(target, exp.Schema):
            for node in target.expressions:
                if isinstance(node, exp.ColumnDef):
                    _column_from_def(dataset, node, dialect)
                else:
                    _apply_table_constraint(dataset, node, dialect)
        query = create.expression
        if query is not None and not dataset.columns:  # CREATE TABLE AS SELECT
            _columns_from_query(dataset, query, dialect)
        _upsert(snapshot, dataset)
    elif kind == "VIEW":
        table = target.this if isinstance(target, exp.Schema) else target
        if not isinstance(table, exp.Table):
            return
        dataset = Dataset(name=_table_name(table), kind="view", description=_table_comment(create))
        query = create.expression
        if query is not None:
            _columns_from_query(dataset, query, dialect)
        _upsert(snapshot, dataset)


def _columns_from_query(dataset: Dataset, query: exp.Expression, dialect: str) -> None:
    select = query if isinstance(query, exp.Select) else query.find(exp.Select)
    if select is not None:
        for projection in select.expressions:
            if isinstance(projection, exp.Star):
                continue
            name = projection.alias_or_name
            if name:
                dataset.columns.append(Column(name=name))
    cte_names = {cte.alias_or_name.lower() for cte in query.find_all(exp.CTE)}
    deps = sorted({_table_name(t) for t in query.find_all(exp.Table) if t.name.lower() not in cte_names})
    dataset.depends_on = deps


def _handle_alter(alter: exp.Alter, dialect: str, snapshot: Snapshot, warnings: list[str], source: str) -> None:
    table = alter.this
    if not isinstance(table, exp.Table):
        return
    dataset = snapshot.get(_table_name(table))
    if dataset is None:
        dataset = Dataset(name=_table_name(table))
        snapshot.add(dataset)
    for action in alter.args.get("actions") or []:
        nodes = action.expressions if isinstance(action, exp.AddConstraint) else [action]
        for node in nodes:
            _apply_table_constraint(dataset, node, dialect)


def _upsert(snapshot: Snapshot, dataset: Dataset) -> None:
    existing = snapshot.get(dataset.name)
    if existing is None or existing.name != dataset.name:
        snapshot.add(dataset)
        return
    # ALTER statements may have created a placeholder before the CREATE was seen.
    existing.kind = dataset.kind
    existing.columns = dataset.columns or existing.columns
    existing.description = existing.description or dataset.description
    existing.primary_key = existing.primary_key or dataset.primary_key
    existing.unique_keys.extend(k for k in dataset.unique_keys if k not in existing.unique_keys)
    existing.foreign_keys.extend(dataset.foreign_keys)
    existing.not_null.extend(c for c in dataset.not_null if c not in existing.not_null)
    existing.depends_on = existing.depends_on or dataset.depends_on


def extract_ddl(paths: Iterable[str | Path], dialect: str) -> tuple[Snapshot, list[str]]:
    """Parse DDL files (or directories of ``*.sql``) into a snapshot."""
    snapshot = Snapshot(dialect=dialect)
    warnings: list[str] = []
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(path.rglob("*.sql")))
        else:
            files.append(path)
    for file in files:
        parse_ddl(file.read_text(encoding="utf-8"), dialect, snapshot, warnings, source=file.as_posix())
        snapshot.sources.append(f"ddl:{file.as_posix()}")
    return snapshot, warnings
