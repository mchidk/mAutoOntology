"""Extractors turn project artifacts into a normalized :class:`~mautoontology.snapshot.Snapshot`."""

from .dbt import extract_dbt
from .ddl import extract_ddl
from .infoschema import extract_infoschema, infoschema_query
from .local import extract_duckdb, extract_sqlite

__all__ = [
    "extract_dbt",
    "extract_ddl",
    "extract_duckdb",
    "extract_infoschema",
    "extract_sqlite",
    "infoschema_query",
]
