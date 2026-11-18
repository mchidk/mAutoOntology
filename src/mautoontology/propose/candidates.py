"""Candidate keys and relationships (before any data is consulted).

Candidates are pruned aggressively before profiling SQL is emitted, because
inclusion checks are pairwise and warehouse queries cost money:

* a parent side must be a candidate key (declared, tested, or id-like and later
  proven unique by profiling);
* types must be compatible;
* names must plausibly match (exact key name, ``<entity>_<key>``, role-playing
  ``<role>_<entity>_id``, shared-prefix conventions such as TPC-H's ``o_custkey``),
  or the column must be an unmatched id-like column (value-only fallback, capped).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..snapshot import Dataset, Snapshot
from .naming import (
    SELF_REFERENCE_HINTS,
    column_prefix,
    id_stem,
    is_id_like,
    similarity,
    strip_prefix,
    table_stems,
)

_COMPATIBLE = {
    "integer": {"integer", "decimal"},
    "decimal": {"integer", "decimal"},
    "string": {"string"},
    "date": {"date"},
    "timestamp": {"timestamp"},
    "timestamp_tz": {"timestamp_tz"},
    "binary": {"binary"},
    "other": {"other", "string", "integer"},
}
_KEYABLE_FAMILIES = {"integer", "decimal", "string", "binary", "other"}


@dataclass
class KeyCandidate:
    dataset: str
    columns: list[str]
    origin: str  # declared | dbt_test | inferred
    proven: bool | None = None  # None = not profiled yet


@dataclass
class RelCandidate:
    child: str
    child_columns: list[str]
    parent: str
    parent_columns: list[str]
    name_score: float
    reason: str
    role: str | None = None
    declared: str | None = None  # declared_fk | dbt_test (origin of a declared fact)
    ref: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, tuple[str, ...], str, tuple[str, ...]]:
        return (self.child, tuple(self.child_columns), self.parent, tuple(self.parent_columns))


def _families_compatible(a: str, b: str) -> bool:
    return b in _COMPATIBLE.get(a, {a})


def profile_columns(profile: dict[str, Any] | None, dataset: str) -> dict[str, Any]:
    if not profile:
        return {}
    return ((profile.get("datasets") or {}).get(dataset) or {}).get("columns") or {}


def is_unique(profile: dict[str, Any] | None, dataset: str, columns: list[str]) -> bool | None:
    """True/False when profiling proved (non-)uniqueness, None when unknown."""
    if not profile:
        return None
    ds = (profile.get("datasets") or {}).get(dataset) or {}
    if [c for c in columns] in (ds.get("unique_keys") or []):
        return True
    if [c for c in columns] in (ds.get("non_unique_keys") or []):
        return False
    if len(columns) == 1:
        col = (ds.get("columns") or {}).get(columns[0])
        rows = ds.get("row_count")
        if col and rows is not None and col.get("distinct") is not None and col.get("non_null") is not None:
            return col["non_null"] == rows and col["distinct"] == rows and rows > 0
    return None


def key_candidates(snapshot: Snapshot, profile: dict[str, Any] | None = None) -> list[KeyCandidate]:
    """Declared, tested and inferred candidate keys for every dataset."""
    out: list[KeyCandidate] = []
    for ds in snapshot.datasets:
        seen: set[tuple[str, ...]] = set()
        for key in ds.keys():
            origin = "declared"
            out.append(KeyCandidate(ds.name, key, origin, True if ds.primary_key == key else None))
            seen.add(tuple(key))
        for col in ds.columns:
            if (col.name,) in seen or col.family not in _KEYABLE_FAMILIES:
                continue
            stems = table_stems(ds.name)
            prefix = column_prefix([c.name for c in ds.columns])
            bare = strip_prefix(col.name, prefix)
            natural_id = bare == "id" or id_stem(bare) in stems or (prefix and bare.endswith("key") and _abbrev_match(id_stem(bare), stems))
            if is_id_like(col.name) or natural_id:
                proven = is_unique(profile, ds.name, [col.name])
                out.append(KeyCandidate(ds.name, [col.name], "inferred", proven))
                seen.add((col.name,))
    return out


def composite_key_candidates(ds: Dataset, max_pairs: int = 6) -> list[list[str]]:
    """Pairs worth testing when a table has no single-column key (bridges, line items, snapshots)."""
    idish = [c.name for c in ds.columns if is_id_like(c.name) and c.family in _KEYABLE_FAMILIES]
    timeish = [c.name for c in ds.columns if c.family in {"date", "timestamp", "timestamp_tz"}]
    pairs: list[list[str]] = []
    for i, a in enumerate(idish):
        for b in idish[i + 1:]:
            pairs.append([a, b])
    for a in idish:
        for t in timeish:
            pairs.append([a, t])
    return pairs[:max_pairs]


def _abbrev_match(stem: str, stems: set[str]) -> bool:
    """``cust`` ~ ``customer``, ``supp`` ~ ``supplier``, ``order`` ~ ``orders``."""
    if not stem:
        return False
    return any(s.startswith(stem) or stem.startswith(s) for s in stems if len(stem) >= 3)


def relationship_candidates(
    snapshot: Snapshot,
    keys: list[KeyCandidate],
    *,
    value_fallback: bool = True,
    max_value_candidates: int = 40,
) -> list[RelCandidate]:
    """Generate pruned relationship candidates (declared facts first)."""
    candidates: dict[tuple, RelCandidate] = {}

    # 1. Declared facts: DDL / catalog foreign keys and dbt relationships tests.
    for ds in snapshot.datasets:
        for fk in ds.foreign_keys:
            parent = snapshot.get(fk.ref_dataset)
            if parent is None:
                continue
            ref_cols = fk.ref_columns if any(fk.ref_columns) else list(parent.primary_key)
            if not ref_cols or len(ref_cols) != len(fk.columns):
                continue
            cand = RelCandidate(
                child=ds.name,
                child_columns=list(fk.columns),
                parent=parent.name,
                parent_columns=list(ref_cols),
                name_score=1.0,
                reason=f"declared ({fk.origin})",
                declared="dbt_test" if fk.origin == "dbt_test" else "declared_fk",
                ref=fk.ref,
            )
            candidates[cand.key] = cand

    # 2. Name-based candidates against single-column candidate keys.
    parent_keys = [k for k in keys if len(k.columns) == 1 and k.proven is not False]
    matched_children: set[tuple[str, str]] = {(c.child, c.child_columns[0]) for c in candidates.values() if len(c.child_columns) == 1}
    for ds in snapshot.datasets:
        prefix = column_prefix([c.name for c in ds.columns])
        own_keys = {tuple(k.columns) for k in keys if k.dataset == ds.name}
        for col in ds.columns:
            if (ds.name, col.name) in matched_children:
                continue
            bare = strip_prefix(col.name, prefix)
            best: list[RelCandidate] = []
            for key in parent_keys:
                parent = snapshot.get(key.dataset)
                if parent is None:
                    continue
                pcol = parent.column(key.columns[0])
                if pcol is None or not _families_compatible(col.family, pcol.family):
                    continue
                if parent.name == ds.name and (col.name,) in own_keys:
                    continue  # a key does not reference itself
                score, reason, role = _name_score(bare, ds, parent, key.columns[0])
                if score <= 0:
                    continue
                best.append(
                    RelCandidate(ds.name, [col.name], parent.name, [key.columns[0]], score, reason, role)
                )
            if best:
                top = max(c.name_score for c in best)
                for cand in best:
                    if cand.name_score >= top - 0.05:
                        candidates[cand.key] = cand
                matched_children.add((ds.name, col.name))

    # 3. Value-only fallback for id-like columns nobody matched by name (capped).
    if value_fallback:
        extra: list[RelCandidate] = []
        for ds in snapshot.datasets:
            own_keys = {tuple(k.columns) for k in keys if k.dataset == ds.name}
            for col in ds.columns:
                if (ds.name, col.name) in matched_children or not is_id_like(col.name) or (col.name,) in own_keys:
                    continue
                self_hint = any(h in col.name for h in SELF_REFERENCE_HINTS)
                for key in parent_keys:
                    parent = snapshot.get(key.dataset)
                    if parent is None:
                        continue
                    pcol = parent.column(key.columns[0])
                    if pcol is None or not _families_compatible(col.family, pcol.family):
                        continue
                    if self_hint and parent.name != ds.name:
                        continue
                    if parent.name == ds.name and not self_hint:
                        continue
                    sim = max(similarity(id_stem(col.name), s) for s in table_stems(parent.name))
                    reason = "self-reference hint" if self_hint else "value-only (unmatched id column)"
                    extra.append(
                        RelCandidate(ds.name, [col.name], parent.name, [key.columns[0]], 0.35 if self_hint else 0.1 * sim, reason,
                                     role=_role_from_hint(col.name) if self_hint else None)
                    )
        extra.sort(key=lambda c: -c.name_score)
        for cand in extra[:max_value_candidates]:
            candidates.setdefault(cand.key, cand)
    return list(candidates.values())


def _role_from_hint(column: str) -> str | None:
    for hint in sorted(SELF_REFERENCE_HINTS, key=len, reverse=True):
        if hint in column:
            return hint
    return None


def _name_score(bare_col: str, child: Dataset, parent: Dataset, parent_key: str) -> tuple[float, str, str | None]:
    """Score how plausibly ``bare_col`` (child column, prefix stripped) references ``parent.parent_key``."""
    parent_prefix = column_prefix([c.name for c in parent.columns])
    key_bare = strip_prefix(parent_key, parent_prefix)
    stems = table_stems(parent.name)
    col_stem = id_stem(bare_col)

    # exact name match of a specific key (customer_id == customer_id; custkey == custkey after prefix strip)
    if bare_col == key_bare and key_bare not in {"id", "key", "code"}:
        if parent.name == child.name:
            return 0.0, "", None
        return 0.9, "exact key-name match", None
    # <entity>_id referencing a generic `id` key
    if key_bare in {"id", "key"} and col_stem in stems:
        return 0.85, "entity-name + generic key", None
    # role-playing: <role>_<key> or <role>_<entity>_id
    for stem in stems:
        for suffix in (f"_{key_bare}", f"_{stem}_id", f"_{stem}_key"):
            if key_bare in {"id", "key"} and suffix == f"_{key_bare}":
                continue
            if bare_col.endswith(suffix) and len(bare_col) > len(suffix):
                role = bare_col[: -len(suffix)].strip("_")
                if role:
                    if parent.name == child.name and role not in SELF_REFERENCE_HINTS:
                        return 0.0, "", None
                    return 0.75, f"role-playing ({role})", role
    # abbreviated stems (TPC-H: custkey ~ customer, suppkey ~ supplier)
    if key_bare.endswith("key") and bare_col.endswith("key") and col_stem and _abbrev_match(col_stem, stems):
        if id_stem(key_bare) == col_stem and parent.name != child.name:
            return 0.8, "abbreviated key-name match", None
    return 0.0, "", None
