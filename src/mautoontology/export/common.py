"""Helpers shared by exporters."""

from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import exp

from ..context import metric_sql
from ..model import Entity, Project
from ..snapshot import norm_ident

ACTIVE = {"draft", "proposed", "approved"}

_OSSIE_TYPES = {
    "integer": "Integer", "decimal": "Decimal", "float": "Float", "string": "String", "boolean": "Boolean",
    "date": "Date", "time": "Time", "timestamp": "DateTime", "timestamp_tz": "DateTimeTz",
}
_SNOWFLAKE_TYPES = {
    "integer": "NUMBER(38,0)", "decimal": "NUMBER", "float": "FLOAT", "string": "VARCHAR", "boolean": "BOOLEAN",
    "date": "DATE", "time": "TIME", "timestamp": "TIMESTAMP_NTZ", "timestamp_tz": "TIMESTAMP_TZ",
}


def ossie_type(family: str | None) -> str:
    return _OSSIE_TYPES.get(family or "", "Opaque")


def snowflake_type(family: str | None) -> str:
    return _SNOWFLAKE_TYPES.get(family or "", "VARCHAR")


def active(element: dict[str, Any]) -> bool:
    return element.get("status", "proposed") in ACTIVE


def attr_expr(attr: dict[str, Any]) -> str:
    return str(attr.get("expr") or attr.get("column") or attr["id"])


def measure_expr(attr: dict[str, Any]) -> str:
    base = attr_expr(attr)
    return f"{base} * {attr['scale']}" if attr.get("scale") else base


def field_name_for_column(entity: Entity, column: str) -> str:
    attr = entity.attribute_for_column(column)
    return attr["id"] if attr else norm_ident(column)


def unqualified_metric_sql(project: Project, metric: dict[str, Any]) -> str | None:
    """Metric aggregate SQL with column references left unqualified (single-table scope)."""
    sql = metric_sql(project, metric)
    if sql is None:
        return None
    try:
        tree = sqlglot.parse_one(sql)
    except sqlglot.errors.ParseError:
        return sql
    for column in tree.find_all(exp.Column):
        column.set("table", None)
    return tree.sql()


def split_dataset(dataset: str) -> tuple[str | None, str | None, str]:
    parts = dataset.split(".")
    if len(parts) >= 3:
        return parts[-3], parts[-2], parts[-1]
    if len(parts) == 2:
        return None, parts[0], parts[1]
    return None, None, parts[0]


def ordered_entities(project: Project) -> list[Entity]:
    return [project.entities[k] for k in sorted(project.entities) if active(project.entities[k].raw)]


def governance(element: dict[str, Any]) -> dict[str, Any]:
    keys = ("status", "origin", "owner", "reviewed_by", "reviewed_at", "evidence", "rationale")
    return {k: element[k] for k in keys if element.get(k) not in (None, [], "")}
