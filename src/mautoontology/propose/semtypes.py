"""Semantic typing of columns: role, semantic type, aggregation and additivity.

The rules are conservative by design. An unknown integer is a *dimension*, not
a measure, because summing a code or a year is a silent, plausible-looking
error. Identifiers are never given a sum/avg aggregation. Price-like and
rate-like numbers are non-additive (averaged), balances on snapshot tables are
semi-additive (never summed over time).
"""

from __future__ import annotations

import re
from typing import Any

from ..snapshot import Column, Dataset
from .naming import is_id_like
from .pii import classify_name

_MONEY = re.compile(r"(amount|revenue|sales|cost|price|fee|tax|discount|total|value|balance|payment|paid|refund|charge|spend|margin|profit|income|budget|salary|cents|gmv|arr|mrr)")
_PRICE_LIKE = re.compile(r"(unit_price|list_price|^price$|_price$|^price_|rate$|_rate_|pct|percent|ratio|score|^avg_|_avg$|average)")
_SEMI_ADDITIVE = re.compile(r"(balance|inventory|stock|on_hand|headcount|level|outstanding|open_amount|backlog)")
_QUANTITY = re.compile(r"(qty|quantity|units|count|num_|_count$|items|volume|weight|duration|seconds|minutes|hours|distance|clicks|views|impressions|sessions)")
_TEXT = re.compile(r"(description|comment|notes?|body|message|text|summary|remarks?|details)$")
_CENTS = re.compile(r"(_cents|_pennies|_minor|_ore|_oere)$")
_CURRENCY_COLS = ("currency", "currency_code", "iso_currency", "currency_iso", "curr", "ccy")
_SEMANTIC_BY_NAME = [
    (re.compile(r"(^|_)e_?mail"), "email"),
    (re.compile(r"(^|_)(phone|mobile|fax)"), "phone"),
    (re.compile(r"(^|_)(country|country_code|country_iso)$"), "country"),
    (re.compile(r"(^|_)city$"), "city"),
    (re.compile(r"(^|_)(postal_code|zip|zip_code|postcode)$"), "postal_code"),
    (re.compile(r"(^|_)(url|website|link)$"), "url"),
    (re.compile(r"(^|_)status$"), "status"),
    (re.compile(r"(^|_)(first_name|last_name|full_name|name)$"), "name"),
]

TIME_FAMILIES = {"date", "timestamp", "timestamp_tz"}
NUMERIC_FAMILIES = {"integer", "decimal", "float"}


def currency_column(dataset: Dataset) -> str | None:
    for col in dataset.columns:
        if col.name in _CURRENCY_COLS or col.name.endswith("_currency"):
            return col.name
    return None


def infer_attribute(
    column: Column,
    dataset: Dataset,
    *,
    key_columns: set[str],
    fk_columns: set[str],
    snapshot_time: str | None,
    profile_col: dict[str, Any],
    row_count: int | None,
) -> dict[str, Any]:
    name = column.name
    attr: dict[str, Any] = {"id": _attr_id(name), "family": column.family}
    if attr["id"] != name:
        attr["column"] = name
    if column.description:
        attr["description"] = column.description

    pii = classify_name(name)
    if pii == "none" and profile_col.get("pii_by_value"):
        pii = "quasi_identifier"
    if pii != "none":
        attr["pii"] = pii

    surrogate_like = name == "id" or name.endswith(("_id", "_key", "_sk", "_uuid", "_guid", "_fk"))
    if name in key_columns or name in fk_columns or (surrogate_like and column.family not in {"decimal", "float"}):
        attr["role"] = "identifier"
        attr["semantic_type"] = "identifier"
    elif column.family in TIME_FAMILIES:
        attr["role"] = "time"
        grains = ["day", "week", "month", "quarter", "year"]
        if column.family != "date":
            grains = ["hour"] + grains
        attr["time"] = {"grains": grains}
        if column.family == "timestamp_tz":
            attr["time"]["timezone"] = "UTC"
    elif column.family in NUMERIC_FAMILIES and _is_measure(name, column.family):
        attr["role"] = "measure"
        _measure_semantics(attr, name, dataset, snapshot_time)
    elif column.family == "string" and _TEXT.search(name):
        attr["role"] = "text"
    elif column.family == "boolean":
        attr["role"] = "dimension"
        attr["semantic_type"] = "flag"
    else:
        attr["role"] = "dimension"

    if "semantic_type" not in attr:
        for pattern, semantic in _SEMANTIC_BY_NAME:
            if pattern.search(name):
                attr["semantic_type"] = semantic
                break

    values = profile_col.get("values")
    accepted = dataset.accepted_values.get(name)
    if attr["role"] == "dimension" and pii == "none":
        if accepted:
            attr["values"] = list(accepted)
            attr["values_complete"] = True
            attr.setdefault("evidence", []).append({"kind": "dbt_test", "detail": "accepted_values"})
        elif values and profile_col.get("values_complete") and column.family != "boolean":
            attr["values"] = [v for v, _ in values if v is not None]
            attr["values_complete"] = True
            attr.setdefault("evidence", []).append({"kind": "profile", "detail": f"{len(attr['values'])} distinct values observed"})
            if len(attr["values"]) <= 50:
                attr["semantic_type"] = attr.get("semantic_type") or "enum"

    if row_count is not None and profile_col.get("non_null") is not None:
        attr["nullable"] = profile_col["non_null"] < row_count
    else:
        attr["nullable"] = column.nullable
    return attr


def _attr_id(column: str) -> str:
    ident = re.sub(r"[^a-z0-9_]", "_", column.lower())
    if not re.match(r"^[a-z]", ident):
        ident = "c_" + ident
    return ident


def _is_measure(name: str, family: str) -> bool:
    if is_id_like(name) or name in {"year", "month", "day", "week", "quarter", "rank", "position", "sort_order", "version", "line_no", "line_number"}:
        return False
    if _MONEY.search(name) or _QUANTITY.search(name) or _PRICE_LIKE.search(name) or _SEMI_ADDITIVE.search(name):
        return True
    return family in {"decimal", "float"}


def _measure_semantics(attr: dict[str, Any], name: str, dataset: Dataset, snapshot_time: str | None) -> None:
    if _PRICE_LIKE.search(name):
        attr["semantic_type"] = "price" if "price" in name else "rate"
        attr["aggregation"] = "avg"
        attr["additivity"] = "non_additive"
    elif _SEMI_ADDITIVE.search(name) or snapshot_time:
        attr["semantic_type"] = "money" if _MONEY.search(name) else "quantity"
        attr["aggregation"] = "sum"
        attr["additivity"] = "semi_additive"
        if snapshot_time:
            attr["non_additive_over"] = snapshot_time
    elif _MONEY.search(name):
        attr["semantic_type"] = "money"
        attr["aggregation"] = "sum"
        attr["additivity"] = "additive"
    else:
        attr["semantic_type"] = "quantity" if _QUANTITY.search(name) else "number"
        attr["aggregation"] = "sum" if _QUANTITY.search(name) else "avg"
        attr["additivity"] = "additive" if _QUANTITY.search(name) else "non_additive"
    if attr.get("semantic_type") in {"money", "price"}:
        currency = currency_column(dataset)
        if currency:
            attr["money"] = {"currency_attr": _attr_id(currency)}
        if _CENTS.search(name):
            attr["scale"] = 0.01
