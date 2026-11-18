"""Propose (or refresh) the semantic layer from evidence, without overwriting human work."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import SCHEMA_ID
from ..model import EVIDENCE_DIR, PROFILE_FILE, PROPOSALS_DIR, SNAPSHOT_FILE, Project
from ..profile import load_profile
from ..snapshot import Snapshot
from ..yamlio import ordered, write_file
from .builder import (
    adopt_dbt_semantics,
    bridge_relationships,
    build_entity,
    entity_ids,
    suggested_metrics,
)
from .candidates import key_candidates, relationship_candidates
from .merge import merge_document
from .relationships import score_candidates

STATE_FILE = "generated.json"


@dataclass
class ProposeSummary:
    entities_created: list[str] = field(default_factory=list)
    entities_updated: list[str] = field(default_factory=list)
    relationships_accepted: int = 0
    relationships_rejected: int = 0
    kept_human: int = 0
    conflicts: int = 0
    review_questions: int = 0
    adopted_metrics: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def run_propose(root: str | Path = ".", *, domain: str | None = None) -> ProposeSummary:
    project = Project.load(root)
    semantics = project.semantics if project.semantics.name == "semantics" else Path(root) / "semantics"
    evidence_dir = semantics / EVIDENCE_DIR
    snapshot_path = evidence_dir / SNAPSHOT_FILE
    if not snapshot_path.is_file():
        raise FileNotFoundError(f"{snapshot_path} not found: run `mautoontology extract` first")
    snapshot = Snapshot.load(snapshot_path)
    profile = load_profile(evidence_dir / PROFILE_FILE)
    if not profile.get("datasets"):
        profile = None
    summary = ProposeSummary()
    if profile is None:
        summary.warnings.append("no profile.json: relationships rely on declared keys/tests only; run `mautoontology profile` for data evidence")

    state_path = evidence_dir / STATE_FILE
    state: dict[str, dict[str, str]] = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    default_domain = domain or (project.config.get("project") or {}).get("default_domain") or "core"

    # Keep ids/paths of entities that already exist (humans may have renamed them).
    ids = entity_ids(snapshot)
    existing_by_dataset = {}
    for entity in project.entities.values():
        ds = snapshot.get(entity.dataset)
        if ds is not None:
            ids[ds.name] = entity.id
            existing_by_dataset[ds.name] = entity

    keys = key_candidates(snapshot, profile)
    candidates = relationship_candidates(snapshot, [k for k in keys if k.proven is not False])
    decision = score_candidates(snapshot, candidates, profile)
    summary.relationships_accepted = len(decision.accepted)
    summary.relationships_rejected = len(decision.rejected)

    outgoing: dict[str, list] = {}
    for rel in decision.accepted:
        outgoing.setdefault(rel.candidate.child, []).append(rel)

    docs: dict[str, dict[str, Any]] = {}
    for ds in snapshot.datasets:
        existing = existing_by_dataset.get(ds.name)
        doc_domain = (existing.domain if existing else None) or default_domain
        docs[ids[ds.name]] = build_entity(ds, snapshot, profile, ids, outgoing.get(ds.name, []), doc_domain)
    bridge_relationships(docs)

    conflicts: list[dict[str, Any]] = []
    new_state: dict[str, dict[str, str]] = {}
    for entity_id, doc in sorted(docs.items()):
        existing = project.entities.get(entity_id)
        path = existing.path if existing else semantics / doc["domain"] / f"{entity_id}.yaml"
        rel = path.relative_to(semantics).as_posix()
        if not path.exists() and rel in state:
            summary.kept_human += 1  # a human deleted this generated file: keep it deleted (tombstone)
            summary.warnings.append(f"{rel} was deleted by a human and is not recreated "
                                    f"(remove its entry from _evidence/{STATE_FILE} to regenerate it)")
            continue
        result = merge_document(path, doc, state.get(rel, {}), kind="entity")
        new_state[rel] = result.state
        if result.created:
            summary.entities_created.append(entity_id)
        elif result.changed:
            summary.entities_updated.append(entity_id)
        summary.kept_human += len(result.kept_human)
        for conflict in result.conflicts:
            conflicts.append({"file": rel, **conflict})

    adopted, dbt_warnings = adopt_dbt_semantics(snapshot, ids)
    summary.warnings.extend(dbt_warnings)
    if adopted:
        path = semantics / "dbt_metrics.yaml"
        rel = path.relative_to(semantics).as_posix()
        doc = {"schema": SCHEMA_ID, "kind": "metrics",
               "description": "Adopted from the dbt Semantic Layer (source of truth stays in dbt).", "metrics": adopted}
        result = merge_document(path, doc, state.get(rel, {}), kind="file", list_keys={"metrics": "metric"})
        new_state[rel] = result.state
        summary.adopted_metrics = len(adopted)
        for conflict in result.conflicts:
            conflicts.append({"file": rel, **conflict})

    # Keep state for files we did not touch this run (e.g. entities whose dataset disappeared).
    for rel, value in state.items():
        new_state.setdefault(rel, value)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes((json.dumps(new_state, indent=2, sort_keys=True) + "\n").encode("utf-8"))

    review = _review_questions(decision, docs, conflicts)
    summary.review_questions = len(review)
    summary.conflicts = len(conflicts)
    proposals = semantics / PROPOSALS_DIR
    write_file(proposals / "review.yaml", ordered({
        "schema": SCHEMA_ID,
        "note": "Ranked questions for a human reviewer. Answer them by editing the semantic files; this file is regenerated.",
        "questions": review,
        "conflicts": conflicts,
    }))
    write_file(proposals / "metrics.yaml", ordered({
        "schema": SCHEMA_ID,
        "note": "Draft metric suggestions. Copy, rename and approve the ones that match a real business definition.",
        "metrics": suggested_metrics(docs),
    }))
    (evidence_dir / "relationships.json").write_bytes((json.dumps({
        "accepted": [{"child": f"{r.candidate.child}.{','.join(r.candidate.child_columns)}",
                      "parent": f"{r.candidate.parent}.{','.join(r.candidate.parent_columns)}",
                      "score": r.score, "cardinality": r.cardinality, "evidence": r.evidence} for r in decision.accepted],
        "rejected": decision.rejected,
    }, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8"))
    return summary


_IMPACT = {"high": 0, "medium": 1, "low": 2}


def _review_questions(decision, docs: dict[str, dict[str, Any]], conflicts: list[dict[str, Any]], limit: int = 15) -> list[dict[str, Any]]:
    questions = list(decision.review)
    resolved = {f"{r.candidate.child}.{','.join(r.candidate.child_columns)}" for r in decision.accepted}
    # A key column that is itself referenced by an accepted relationship is a parent, not an orphan child.
    resolved |= {f"{r.candidate.parent}.{','.join(r.candidate.parent_columns)}" for r in decision.accepted}
    asked: set[str] = set()
    for rej in decision.rejected:
        if rej["child"] in resolved or rej["child"] in asked:
            continue
        if "below" in rej["reason"] and rej.get("name_evidence", "").startswith(("exact", "role", "entity", "abbrev")):
            asked.add(rej["child"])
            questions.append({
                "question": f"{rej['child']} is named like a key of {rej['parent']} but only {rej.get('ratio', 0):.0%} of its values match. "
                            "Is it a different code system, a stale reference, or a data-quality problem?",
                "kind": "name_matched_but_data_disagrees", "impact": "medium"})
    for entity in docs.values():
        if not entity.get("grain"):
            questions.append({"question": f"What does one row of {entity['source']['dataset']} represent? No unique key was found.",
                              "kind": "missing_grain", "impact": "high"})
        for attr in entity.get("attributes", []):
            if attr.get("semantic_type") in {"money", "price"} and not attr.get("money"):
                questions.append({"question": f"Which currency is {entity['id']}.{attr['id']} in?", "kind": "currency_unknown", "impact": "medium"})
    for conflict in conflicts:
        questions.append({"question": f"The generator now proposes a different version of {conflict['file']} {conflict['element']} "
                                      "than the human-edited one. Review the proposed changes.",
                          "kind": "conflict", "impact": "low"})
    questions.sort(key=lambda q: _IMPACT.get(q.get("impact", "low"), 3))
    return questions[:limit]
