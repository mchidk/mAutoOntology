"""Score relationship candidates against evidence and select a consistent set.

Scores come only from evidence, never from a model's opinion:

* declared foreign keys (DDL/catalog) and dbt ``relationships`` tests;
* inclusion ratio (share of distinct child values found in the parent key),
  discounted when there are few distinct values;
* proven uniqueness of the parent key (otherwise it is not a key at all);
* name evidence (exact key name, role-playing pattern, shared prefixes).

Selection is global, in the spirit of schema-graph approaches: every child
column keeps at most one parent (the best supported), symmetric one-to-one
pairs keep a single direction, and ambiguous value-only matches are not
accepted but reported for review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..snapshot import Snapshot
from .candidates import RelCandidate, is_unique
from .naming import id_stem, table_stems

ACCEPT_RATIO = 0.95
VALUE_ONLY_RATIO = 0.99
MIN_VALUE_ONLY_DISTINCT = 20


@dataclass
class ScoredRelationship:
    candidate: RelCandidate
    score: float
    ratio: float | None
    cardinality: str
    optional: bool | None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)


@dataclass
class RelationshipDecision:
    accepted: list[ScoredRelationship]
    review: list[dict[str, Any]]
    rejected: list[dict[str, Any]]


def _inclusion(profile: dict[str, Any] | None, cand: RelCandidate) -> dict[str, Any] | None:
    if not profile:
        return None
    for inc in profile.get("inclusions") or []:
        if (inc["child"], inc["child_columns"], inc["parent"], inc["parent_columns"]) == (
            cand.child, cand.child_columns, cand.parent, cand.parent_columns):
            return inc
    return None


def _confidence_for_distinct(distinct: int) -> float:
    return min(1.0, 0.6 + distinct / 50.0)


def score_candidates(
    snapshot: Snapshot,
    candidates: list[RelCandidate],
    profile: dict[str, Any] | None,
) -> RelationshipDecision:
    scored: list[ScoredRelationship] = []
    review: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for cand in candidates:
        evidence: list[dict[str, Any]] = []
        caveats: list[str] = []
        inc = _inclusion(profile, cand)
        ratio = inc.get("ratio") if inc else None
        distinct = (inc or {}).get("child_distinct") or 0
        parent_unique = is_unique(profile, cand.parent, cand.parent_columns)
        parent_ds = snapshot.get(cand.parent)
        if parent_ds is not None and cand.parent_columns in parent_ds.keys():
            parent_unique = True if parent_unique is None else parent_unique

        if cand.declared:
            base = 1.0 if cand.declared == "declared_fk" else 0.98
            evidence.append({"kind": "declared_fk" if cand.declared == "declared_fk" else "dbt_test",
                             **({"ref": cand.ref} if cand.ref else {}), "score": base})
            score = base
            if ratio is not None:
                evidence.append(_inclusion_evidence(inc))
                if ratio < ACCEPT_RATIO:
                    caveats.append(f"declared, but only {ratio:.1%} of distinct child values exist in the parent (orphans)")
        else:
            if inc is None:
                if profile and profile.get("stage2"):
                    rejected.append(_reject(cand, "pruned before profiling (insufficient name or value evidence)"))
                elif cand.name_score >= 0.5:
                    review.append(_review_item(cand, "not profiled yet: run `mautoontology profile` to confirm"))
                continue
            if parent_unique is False:
                rejected.append(_reject(cand, "parent columns are not unique"))
                continue
            if ratio is None or distinct == 0:
                rejected.append(_reject(cand, "child column is empty"))
                continue
            name_backed = cand.name_score >= 0.5 or "self-reference" in cand.reason
            threshold = ACCEPT_RATIO if name_backed else VALUE_ONLY_RATIO
            if ratio < threshold:
                rejected.append(_reject(cand, f"inclusion {ratio:.1%} below {threshold:.0%}", ratio))
                continue
            if not name_backed and distinct < MIN_VALUE_ONLY_DISTINCT:
                rejected.append(_reject(cand, f"value-only match on only {distinct} distinct values", ratio))
                continue
            incl_score = ratio * _confidence_for_distinct(distinct)
            score = round(0.45 * cand.name_score + 0.55 * incl_score, 3) if name_backed else round(0.6 * incl_score, 3)
            evidence.append(_inclusion_evidence(inc))
            if parent_unique:
                evidence.append({"kind": "uniqueness", "ref": f"{cand.parent}({', '.join(cand.parent_columns)})", "score": 1.0})
            if cand.name_score > 0:
                evidence.append({"kind": "name_match", "detail": cand.reason, "score": round(cand.name_score, 2)})
            if ratio < 1.0:
                caveats.append(f"{1 - ratio:.1%} of distinct child values have no parent row (orphans); use LEFT JOIN to keep them")

        child_unique = is_unique(profile, cand.child, cand.child_columns)
        child_ds = snapshot.get(cand.child)
        if child_ds is not None and cand.child_columns in child_ds.keys():
            child_unique = True
        cardinality = "one_to_one" if child_unique else "many_to_one"
        non_null = (inc or {}).get("child_non_null")
        rows = (inc or {}).get("child_rows")
        optional = (non_null < rows) if (non_null is not None and rows is not None) else None
        if optional is None and child_ds is not None:
            col = child_ds.column(cand.child_columns[0])
            optional = col.nullable if col else None
        scored.append(ScoredRelationship(cand, score, ratio, cardinality, optional, evidence, caveats))

    accepted = _select(scored, review)
    return RelationshipDecision(accepted, review, rejected)


def _select(scored: list[ScoredRelationship], review: list[dict[str, Any]]) -> list[ScoredRelationship]:
    # 1. At most one parent per child column set.
    by_child: dict[tuple[str, tuple[str, ...]], list[ScoredRelationship]] = {}
    for rel in scored:
        by_child.setdefault((rel.candidate.child, tuple(rel.candidate.child_columns)), []).append(rel)
    chosen: list[ScoredRelationship] = []
    for (child, cols), options in by_child.items():
        options.sort(key=lambda r: (-r.score, -(r.ratio or 0)))
        best = options[0]
        contenders = [o for o in options[1:] if abs(o.score - best.score) < 0.02 and not best.candidate.declared]
        if contenders and best.candidate.name_score < 0.5:
            review.append({
                "question": f"{child}.{', '.join(cols)} matches several parents equally well by value: "
                            + ", ".join(f"{o.candidate.parent}.{o.candidate.parent_columns[0]}" for o in [best, *contenders])
                            + ". Which one (if any) does it reference?",
                "kind": "ambiguous_relationship",
                "impact": "high",
            })
            continue
        chosen.append(best)
    # 2. Symmetric one-to-one pairs: keep one direction.
    keyed = {(r.candidate.child, tuple(r.candidate.child_columns), r.candidate.parent, tuple(r.candidate.parent_columns)): r for r in chosen}
    result: list[ScoredRelationship] = []
    dropped: set[int] = set()
    for key, rel in keyed.items():
        mirror = keyed.get((key[2], key[3], key[0], key[1]))
        if mirror is None or id(rel) in dropped:
            if id(rel) not in dropped:
                result.append(rel)
            continue
        keep = _preferred_direction(rel, mirror)
        drop = mirror if keep is rel else rel
        dropped.add(id(drop))
        if keep not in result:
            result.append(keep)
    return result


def _preferred_direction(a: ScoredRelationship, b: ScoredRelationship) -> ScoredRelationship:
    """For A.x <-> B.y (both unique), the parent is the table the key 'belongs to'."""
    def belongs(rel: ScoredRelationship) -> bool:
        return id_stem(rel.candidate.parent_columns[0]) in table_stems(rel.candidate.parent)
    if a.candidate.declared and not b.candidate.declared:
        return a
    if b.candidate.declared and not a.candidate.declared:
        return b
    if belongs(a) != belongs(b):
        return a if belongs(a) else b
    return a if a.score >= b.score else b


def _inclusion_evidence(inc: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "inclusion",
        "score": round(inc["ratio"], 4) if inc.get("ratio") is not None else 0.0,
        "detail": f"{inc['matched_distinct']}/{inc['child_distinct']} distinct values found in parent",
    }


def _review_item(cand: RelCandidate, why: str) -> dict[str, Any]:
    return {
        "question": f"Does {cand.child}.{', '.join(cand.child_columns)} reference {cand.parent}.{', '.join(cand.parent_columns)}? ({why})",
        "kind": "unconfirmed_relationship",
        "impact": "medium",
    }


def _reject(cand: RelCandidate, why: str, ratio: float | None = None) -> dict[str, Any]:
    return {
        "child": f"{cand.child}.{', '.join(cand.child_columns)}",
        "parent": f"{cand.parent}.{', '.join(cand.parent_columns)}",
        "reason": why,
        "name_evidence": cand.reason,
        **({"ratio": ratio} if ratio is not None else {}),
    }
