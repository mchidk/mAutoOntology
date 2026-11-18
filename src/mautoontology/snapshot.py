"""Normalized physical catalog ("snapshot") shared by every extractor.

A snapshot is the warehouse-agnostic description of what physically exists:
datasets (tables, views, dbt models), their columns with portable type families,
declared keys, and foreign keys with the origin of each fact. Extractors produce
snapshots; profiling and proposal consume them. Snapshots are plain JSON so any
agent or tool can read them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SNAPSHOT_VERSION = 1

#: Portable type families. Exporters map these to each target's type system.
TYPE_FAMILIES = (
    "integer",
    "decimal",
    "float",
    "string",
    "boolean",
    "date",
    "time",
    "timestamp",
    "timestamp_tz",
    "binary",
    "json",
    "array",
    "other",
)

_TYPE_PATTERNS: list[tuple[str, str]] = [
    (r"^(timestamp_tz|timestamptz|timestamp with time zone|datetimeoffset|timestamp_ltz)", "timestamp_tz"),
    (r"^(timestamp|datetime|datetime2|smalldatetime|timestamp_ntz|timestamp without time zone)", "timestamp"),
    (r"^date$", "date"),
    (r"^time", "time"),
    (r"^(bool|boolean|bit)$", "boolean"),
    (r"^(tinyint|smallint|int|integer|bigint|hugeint|int\d+|uint\d+|ubigint|uinteger|usmallint|utinyint|long|short|byte|serial|bigserial|smallserial)$", "integer"),
    (r"^(number|numeric|decimal|dec|money|smallmoney|bignumeric|bigdecimal)", "decimal"),
    (r"^(float|double|real|float\d+|double precision|binary_float|binary_double)", "float"),
    (r"^(varchar|char|character|nvarchar|nchar|text|string|ntext|clob|citext|uuid|uniqueidentifier|tinytext|mediumtext|longtext|enum|character varying|bpchar)", "string"),
    (r"^(binary|varbinary|blob|bytea|bytes|image)", "binary"),
    (r"^(json|jsonb|variant|object|struct|map|super)", "json"),
    (r"^(array|list)", "array"),
]


#: Dialects where a bare TIMESTAMP is an instant (time-zone aware).
_TZ_TIMESTAMP_DIALECTS = {"databricks", "spark", "bigquery", "hive"}


def type_family(raw_type: str | None, dialect: str | None = None) -> str:
    """Map a raw warehouse type (any dialect) to a portable type family."""
    if not raw_type:
        return "other"
    text = raw_type.strip().lower()
    text = re.sub(r"\s+", " ", text)
    # NUMBER(p, 0) / DECIMAL(p, 0) are integers in practice (Snowflake ids).
    if re.match(r"^(number|numeric|decimal)\s*\(\s*\d+\s*,\s*0\s*\)$", text):
        return "integer"
    if text == "timestamp" and (dialect or "").lower() in _TZ_TIMESTAMP_DIALECTS:
        return "timestamp_tz"
    for pattern, family in _TYPE_PATTERNS:
        if re.match(pattern, text):
            return family
    return "other"


def norm_ident(name: str) -> str:
    """Normalize an identifier for matching (case-insensitive, unquoted)."""
    return name.strip().strip('"`[]').lower()


def norm_dataset(name: str) -> str:
    """Normalize a possibly qualified dataset name (``db.schema.table``)."""
    return ".".join(norm_ident(part) for part in name.split(".") if part)


@dataclass
class Column:
    name: str
    data_type: str = ""
    family: str = "other"
    nullable: bool = True
    description: str | None = None

    def __post_init__(self) -> None:
        self.name = norm_ident(self.name)
        if self.family == "other" and self.data_type:
            self.family = type_family(self.data_type)

    def refine_family(self, dialect: str) -> None:
        """Re-derive the family with dialect-specific rules (e.g. bare TIMESTAMP)."""
        if self.data_type:
            self.family = type_family(self.data_type, dialect)


@dataclass
class ForeignKey:
    columns: list[str]
    ref_dataset: str
    ref_columns: list[str]
    #: Where the fact came from: ddl | catalog | dbt_test | dbt_semantic.
    origin: str = "ddl"
    ref: str | None = None

    def __post_init__(self) -> None:
        self.columns = [norm_ident(c) for c in self.columns]
        self.ref_columns = [norm_ident(c) for c in self.ref_columns]
        self.ref_dataset = norm_dataset(self.ref_dataset)


@dataclass
class Dataset:
    name: str
    kind: str = "table"  # table | view | model | seed | snapshot | source
    columns: list[Column] = field(default_factory=list)
    description: str | None = None
    primary_key: list[str] = field(default_factory=list)
    unique_keys: list[list[str]] = field(default_factory=list)
    not_null: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    accepted_values: dict[str, list[Any]] = field(default_factory=dict)
    row_count: int | None = None
    depends_on: list[str] = field(default_factory=list)
    dbt_unique_id: str | None = None

    def __post_init__(self) -> None:
        self.name = norm_dataset(self.name)
        self.primary_key = [norm_ident(c) for c in self.primary_key]
        self.unique_keys = [[norm_ident(c) for c in key] for key in self.unique_keys]
        self.not_null = [norm_ident(c) for c in self.not_null]

    @property
    def short_name(self) -> str:
        return self.name.split(".")[-1]

    def column(self, name: str) -> Column | None:
        wanted = norm_ident(name)
        for col in self.columns:
            if col.name == wanted:
                return col
        return None

    def keys(self) -> list[list[str]]:
        """All declared candidate keys (primary first)."""
        out: list[list[str]] = []
        if self.primary_key:
            out.append(list(self.primary_key))
        for key in self.unique_keys:
            if key and key not in out:
                out.append(list(key))
        return out


@dataclass
class Snapshot:
    dialect: str = "ansi"
    sources: list[str] = field(default_factory=list)
    datasets: list[Dataset] = field(default_factory=list)
    #: Existing semantic definitions found in the project, e.g.
    #: ``{"dbt": {"semantic_models": [...], "metrics": [...]}}``. They are
    #: adopted (imported read-only), never duplicated.
    semantic: dict[str, Any] = field(default_factory=dict)
    version: int = SNAPSHOT_VERSION

    # ------------------------------------------------------------------ lookup
    def get(self, name: str) -> Dataset | None:
        """Find a dataset by full or partially qualified name (unique suffix match)."""
        wanted = norm_dataset(name)
        exact = [d for d in self.datasets if d.name == wanted]
        if exact:
            return exact[0]
        suffix = [d for d in self.datasets if d.name.endswith("." + wanted) or wanted.endswith("." + d.name)]
        if len(suffix) == 1:
            return suffix[0]
        short = wanted.split(".")[-1]
        by_short = [d for d in self.datasets if d.short_name == short]
        return by_short[0] if len(by_short) == 1 else None

    def add(self, dataset: Dataset) -> None:
        self.datasets.append(dataset)

    # --------------------------------------------------------------- serialize
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["datasets"] = sorted(data["datasets"], key=lambda d: d["name"])
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=False, default=str) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Snapshot:
        datasets = []
        for raw in data.get("datasets", []):
            raw = dict(raw)
            raw["columns"] = [Column(**c) for c in raw.get("columns", [])]
            raw["foreign_keys"] = [ForeignKey(**fk) for fk in raw.get("foreign_keys", [])]
            datasets.append(Dataset(**raw))
        return cls(
            dialect=data.get("dialect", "ansi"),
            sources=list(data.get("sources", [])),
            datasets=datasets,
            semantic=dict(data.get("semantic") or {}),
            version=int(data.get("version", SNAPSHOT_VERSION)),
        )

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.to_json().encode("utf-8"))  # LF on every OS: clean git diffs
        return target

    @classmethod
    def load(cls, path: str | Path) -> Snapshot:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def merge_snapshots(snapshots: Iterable[Snapshot]) -> Snapshot:
    """Merge snapshots from several extractors into one.

    Datasets are matched by name (falling back to a unique ``schema.table`` or
    table-name match). Later snapshots fill gaps but never erase facts:
    descriptions/types fill when missing, keys and foreign keys are unioned.
    This is how a dbt manifest (descriptions, tests, lineage) combines with an
    information_schema export (authoritative types).
    """
    merged = Snapshot()
    for snap in snapshots:
        if merged.dialect == "ansi" and snap.dialect != "ansi":
            merged.dialect = snap.dialect
        merged.sources.extend(s for s in snap.sources if s not in merged.sources)
        for origin, payload in snap.semantic.items():
            merged.semantic.setdefault(origin, payload)
        for ds in snap.datasets:
            target = merged.get(ds.name)
            if target is None:
                merged.add(ds)
                continue
            _merge_dataset(target, ds)
    return merged


def _merge_dataset(target: Dataset, other: Dataset) -> None:
    if len(other.name.split(".")) > len(target.name.split(".")):
        target.name = other.name
    if target.kind == "table" and other.kind != "table":
        target.kind = other.kind
    target.description = target.description or other.description
    target.row_count = target.row_count if target.row_count is not None else other.row_count
    target.dbt_unique_id = target.dbt_unique_id or other.dbt_unique_id
    for dep in other.depends_on:
        if dep not in target.depends_on:
            target.depends_on.append(dep)
    for col in other.columns:
        existing = target.column(col.name)
        if existing is None:
            target.columns.append(col)
            continue
        if not existing.data_type and col.data_type:
            existing.data_type, existing.family = col.data_type, col.family
        existing.description = existing.description or col.description
        existing.nullable = existing.nullable and col.nullable
    if not target.primary_key and other.primary_key:
        target.primary_key = list(other.primary_key)
    for key in other.unique_keys:
        if key not in target.unique_keys:
            target.unique_keys.append(key)
    for col in other.not_null:
        if col not in target.not_null:
            target.not_null.append(col)
    existing_fks = {(tuple(fk.columns), fk.ref_dataset, tuple(fk.ref_columns)) for fk in target.foreign_keys}
    for fk in other.foreign_keys:
        key = (tuple(fk.columns), fk.ref_dataset, tuple(fk.ref_columns))
        if key not in existing_fks:
            target.foreign_keys.append(fk)
            existing_fks.add(key)
    for col, values in other.accepted_values.items():
        target.accepted_values.setdefault(col, values)
