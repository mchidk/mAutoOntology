"""Load a semantic layer (a directory of YAML files) into an indexed, read-only view.

The YAML files are the source of truth. ``Project`` only indexes them so tools
can look up entities, attributes, relationships, metrics, glossary terms and
verified queries by id. Nothing here writes files.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

from . import SCHEMA_ID
from .snapshot import Snapshot, norm_ident
from .yamlio import load_file, plain

SEMANTICS_DIR = "semantics"
CONFIG_FILE = "mautoontology.yaml"
EVIDENCE_DIR = "_evidence"
PROPOSALS_DIR = "_proposals"
SNAPSHOT_FILE = "snapshot.json"
PROFILE_FILE = "profile.json"
SKIP_DIRS = {EVIDENCE_DIR, PROPOSALS_DIR, ".cache"}

ACTIVE_STATUSES = {"draft", "proposed", "approved"}


@cache
def load_schema(name: str = "mautoontology.v1.schema.json") -> dict[str, Any]:
    """Load a bundled JSON schema."""
    text = resources.files("mautoontology").joinpath("schema", name).read_text(encoding="utf-8")
    return json.loads(text)


@dataclass
class Relationship:
    """A relationship resolved to its owning (``from``) entity."""

    id: str
    from_entity: str
    to_entity: str
    cardinality: str
    pairs: list[tuple[str, str]]
    role: str | None = None
    optional: bool | None = None
    through: str | None = None
    condition: str | None = None
    as_of: dict[str, Any] | None = None
    status: str = "proposed"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES


@dataclass
class Entity:
    id: str
    raw: dict[str, Any]
    path: Path

    @property
    def dataset(self) -> str:
        return str(self.raw.get("source", {}).get("dataset", ""))

    @property
    def domain(self) -> str | None:
        return self.raw.get("domain")

    @property
    def status(self) -> str:
        return self.raw.get("status", "proposed")

    @property
    def attributes(self) -> list[dict[str, Any]]:
        return list(self.raw.get("attributes") or [])

    def attribute(self, attr_id: str) -> dict[str, Any] | None:
        wanted = norm_ident(attr_id)
        for attr in self.attributes:
            if attr.get("id") == wanted:
                return attr
        for attr in self.attributes:  # fall back to physical column name
            if norm_ident(str(attr.get("column", ""))) == wanted:
                return attr
        return None

    def attribute_for_column(self, column: str) -> dict[str, Any] | None:
        wanted = norm_ident(column)
        for attr in self.attributes:
            if norm_ident(str(attr.get("column") or attr.get("id"))) == wanted:
                return attr
        return None

    def column_of(self, attr_id: str) -> str | None:
        attr = self.attribute(attr_id)
        if attr is None:
            return None
        if attr.get("expr"):
            return None
        return norm_ident(str(attr.get("column") or attr["id"]))

    @property
    def primary_key(self) -> list[str]:
        keys = self.raw.get("keys") or {}
        return [norm_ident(c) for c in keys.get("primary") or self.raw.get("grain") or []]

    def candidate_keys(self) -> list[list[str]]:
        keys = self.raw.get("keys") or {}
        out = []
        if self.primary_key:
            out.append(self.primary_key)
        for key in keys.get("unique") or []:
            out.append([norm_ident(c) for c in key])
        return out

    def is_key(self, columns: list[str]) -> bool:
        cols = sorted(norm_ident(c) for c in columns)
        return any(sorted(k) == cols for k in self.candidate_keys())


@dataclass
class Project:
    root: Path
    semantics: Path
    config: dict[str, Any] = field(default_factory=dict)
    files: dict[Path, dict[str, Any]] = field(default_factory=dict)
    entities: dict[str, Entity] = field(default_factory=dict)
    metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    metric_files: dict[str, Path] = field(default_factory=dict)
    terms: dict[str, dict[str, Any]] = field(default_factory=dict)
    queries: dict[str, dict[str, Any]] = field(default_factory=dict)
    load_errors: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    _snapshot: Snapshot | None = None

    # ----------------------------------------------------------------- loading
    @classmethod
    def load(cls, root: str | Path = ".") -> Project:
        root_path = Path(root).resolve()
        semantics = root_path / SEMANTICS_DIR if (root_path / SEMANTICS_DIR).is_dir() else root_path
        project = cls(root=root_path, semantics=semantics)
        for path in sorted(project.iter_files()):
            try:
                data = plain(load_file(path))
            except Exception as error:  # malformed YAML is reported, never fatal
                project.load_errors.append(f"{project.rel(path)}: YAML parse error: {error}")
                continue
            if not isinstance(data, dict) or data.get("schema") != SCHEMA_ID:
                continue  # not one of ours (e.g. a dbt schema.yml living nearby)
            project.files[path] = data
            project._index(path, data)
        return project

    def iter_files(self) -> Iterator[Path]:
        if not self.semantics.is_dir():
            return
        for path in self.semantics.rglob("*"):
            if path.suffix not in {".yaml", ".yml"} or not path.is_file():
                continue
            rel_parts = path.relative_to(self.semantics).parts
            if any(part in SKIP_DIRS for part in rel_parts[:-1]):
                continue
            yield path

    def rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def _index(self, path: Path, data: dict[str, Any]) -> None:
        kind = data.get("kind")
        if kind == "config":
            self.config = data
        elif kind == "entity":
            entity_id = str(data.get("id", ""))
            if entity_id in self.entities:
                self.duplicates.append(f"entity '{entity_id}' defined in {self.rel(self.entities[entity_id].path)} and {self.rel(path)}")
            self.entities[entity_id] = Entity(entity_id, data, path)
        elif kind == "metrics":
            for metric in data.get("metrics") or []:
                metric = dict(metric)
                metric.setdefault("domain", data.get("domain"))
                metric_id = str(metric.get("id", ""))
                if metric_id in self.metrics:
                    self.duplicates.append(f"metric '{metric_id}' defined twice ({self.rel(self.metric_files[metric_id])}, {self.rel(path)})")
                self.metrics[metric_id] = metric
                self.metric_files[metric_id] = path
        elif kind == "glossary":
            for term in data.get("terms") or []:
                term_id = str(term.get("id", ""))
                if term_id in self.terms:
                    self.duplicates.append(f"glossary term '{term_id}' defined twice")
                self.terms[term_id] = dict(term)
        elif kind == "verified_queries":
            for query in data.get("queries") or []:
                query_id = str(query.get("id", ""))
                if query_id in self.queries:
                    self.duplicates.append(f"verified query '{query_id}' defined twice")
                self.queries[query_id] = dict(query)

    # ----------------------------------------------------------------- helpers
    @property
    def dialect(self) -> str:
        return str((self.config.get("project") or {}).get("dialect") or self.snapshot_dialect or "ansi")

    @property
    def snapshot_dialect(self) -> str | None:
        snap = self.snapshot
        return snap.dialect if snap else None

    @property
    def snapshot(self) -> Snapshot | None:
        if self._snapshot is None:
            path = self.semantics / EVIDENCE_DIR / SNAPSHOT_FILE
            if path.is_file():
                self._snapshot = Snapshot.load(path)
        return self._snapshot

    def relationships(self, include_inactive: bool = False) -> list[Relationship]:
        out: list[Relationship] = []
        for entity in self.entities.values():
            for raw in entity.raw.get("relationships") or []:
                rel = Relationship(
                    id=str(raw.get("id")),
                    from_entity=entity.id,
                    to_entity=str(raw.get("to")),
                    cardinality=str(raw.get("cardinality", "many_to_one")),
                    pairs=[(norm_ident(str(p["from"])), norm_ident(str(p["to"]))) for p in raw.get("on") or []],
                    role=raw.get("role"),
                    optional=raw.get("optional"),
                    through=raw.get("through"),
                    condition=raw.get("condition"),
                    as_of=raw.get("as_of"),
                    status=str(raw.get("status", "proposed")),
                    raw=dict(raw),
                )
                if include_inactive or rel.active:
                    out.append(rel)
        return out

    def entity_for_dataset(self, dataset: str) -> Entity | None:
        """Map a (possibly partially qualified) physical table name to its entity."""
        wanted = dataset.lower().strip('"`[]')
        exact = [e for e in self.entities.values() if e.dataset.lower() == wanted]
        if exact:
            return exact[0]
        snap = self.snapshot
        if snap is not None and "." in wanted:
            physical = next((d for d in snap.datasets if d.name == wanted), None)
            if physical is not None and not any(e.dataset.lower() == physical.name for e in self.entities.values()):
                return None  # e.g. staging.orders is a different table than the modelled orders
        short = wanted.split(".")[-1]
        matches = [
            e for e in self.entities.values()
            if e.dataset.lower().endswith("." + wanted) or wanted.endswith("." + e.dataset.lower())
            or e.dataset.lower().split(".")[-1] == short
        ]
        return matches[0] if len(matches) == 1 else None

    def entity_columns(self, entity: Entity) -> set[str]:
        """Physical columns known for an entity (snapshot first, then attributes)."""
        cols: set[str] = set()
        snap = self.snapshot
        if snap:
            ds = snap.get(entity.dataset)
            if ds:
                cols.update(c.name for c in ds.columns)
        for attr in entity.attributes:
            if not attr.get("expr"):
                cols.add(norm_ident(str(attr.get("column") or attr.get("id"))))
        return cols

    def physical_columns(self, entity: Entity) -> set[str] | None:
        """Columns the catalog snapshot knows for the entity's dataset (None when not catalogued)."""
        snap = self.snapshot
        ds = snap.get(entity.dataset) if snap else None
        return {c.name for c in ds.columns} if ds is not None and ds.columns else None

    def resolve_ref(self, ref: str) -> bool:
        """Check that a typed reference such as ``metric:net_revenue`` exists."""
        kind, _, target = ref.partition(":")
        if kind == "metric":
            return target in self.metrics
        if kind == "entity":
            return target in self.entities
        if kind in {"attribute", "segment"}:
            entity_id, _, child = target.partition(".")
            entity = self.entities.get(entity_id)
            if entity is None or not child:
                return False
            if kind == "attribute":
                return entity.attribute(child) is not None
            return any(s.get("id") == child for s in entity.raw.get("segments") or [])
        if kind == "relationship":
            return any(r.id == target for r in self.relationships(include_inactive=True))
        return False
