"""Name heuristics shared by key, relationship and semantic-type inference."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

ID_SUFFIXES = ("_id", "_key", "_sk", "_fk", "_code", "_no", "_num", "_number", "_uuid", "_guid", "id", "key")
SELF_REFERENCE_HINTS = {
    "parent", "manager", "supervisor", "reports_to", "boss", "lead", "mentor", "referrer", "referred_by",
    "predecessor", "successor", "previous", "next", "original", "root", "master", "merged_into", "replaced_by",
}


def singular(word: str) -> str:
    """Very small English singularizer good enough for table names."""
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "shes", "ches", "xes", "zes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")) and len(word) > 3:
        return word[:-1]
    return word


def table_stems(table: str) -> set[str]:
    """Plausible entity stems for a table name (``dim_customers`` -> {customer, customers})."""
    short = table.split(".")[-1].lower()
    stems = {short, singular(short)}
    stripped = re.sub(r"^(dim|fct|fact|stg|int|src|raw|tbl|t|d|f|v|vw|mart|base|ref|lkp|lookup)_", "", short)
    stripped = re.sub(r"_(dim|fact|tbl|table|view|v|vw|history|hist|snapshot|current|latest)$", "", stripped)
    stems |= {stripped, singular(stripped)}
    return {s for s in stems if s}


def column_prefix(columns: list[str]) -> str | None:
    """Detect a shared column prefix such as TPC-H's ``l_`` / ``ps_`` convention."""
    if len(columns) < 2:
        return None
    first = columns[0]
    match = re.match(r"^([a-z]{1,4}_)", first)
    if not match:
        return None
    prefix = match.group(1)
    if all(c.startswith(prefix) for c in columns):
        return prefix
    return None


def strip_prefix(column: str, prefix: str | None) -> str:
    return column[len(prefix):] if prefix and column.startswith(prefix) else column


def is_id_like(column: str) -> bool:
    col = column.lower()
    return col == "id" or col.endswith(ID_SUFFIXES)


def id_stem(column: str) -> str:
    """``customer_id`` -> ``customer``; ``custkey`` -> ``cust``; ``id`` -> ``''``."""
    col = column.lower()
    for suffix in ("_id", "_key", "_sk", "_fk", "_uuid", "_guid", "_code", "_no", "_num", "_number", "id", "key"):
        if col.endswith(suffix):
            return col[: -len(suffix)].rstrip("_")
    return col


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def humanize(identifier: str) -> str:
    return identifier.replace("_", " ").strip().capitalize()
