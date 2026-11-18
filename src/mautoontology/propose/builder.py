"""Build proposed entity / metric documents from a snapshot, a profile and scored relationships."""

from __future__ import annotations

import re
from typing import Any

from .. import SCHEMA_ID
from ..snapshot import Dataset, Snapshot
from .candidates import is_unique, key_candidates
from .naming import singular, table_stems
from .relationships import ScoredRelationship
from .semtypes import TIME_FAMILIES, infer_attribute

_VALID_FROM = ("valid_from", "effective_from", "effective_start", "start_date", "dbt_valid_from", "valid_start", "from_date")
_VALID_TO = ("valid_to", "effective_to", "effective_end", "end_date", "dbt_valid_to", "valid_end", "to_date")
_CURRENT_FLAG = ("is_current", "current_flag", "is_latest", "current_ind")
_SOFT_DELETE_BOOL = ("is_deleted", "deleted", "_fivetran_deleted", "is_removed", "deleted_flag")
_SOFT_DELETE_TS = ("deleted_at", "deleted_on", "deleted_date", "removed_at")


def entity_ids(snapshot: Snapshot) -> dict[str, str]:
    """Stable entity ids per dataset (singular, prefix-stripped, collision-safe)."""
    proposed: dict[str, str] = {}
    for ds in sorted(snapshot.datasets, key=lambda d: d.name):
        short = ds.short_name
        stripped = re.sub(r"^(dim|fct|fact|stg|int|src|raw|tbl|mart|base)_", "", short)
        candidate = re.sub(r"[^a-z0-9_]", "_", singular(stripped)) or "entity"
        if not re.match(r"^[a-z]", candidate):
            candidate = "e_" + candidate
        proposed[ds.name] = candidate
    counts: dict[str, int] = {}
    for value in proposed.values():
        counts[value] = counts.get(value, 0) + 1
    result = {}
    for name, value in proposed.items():
        if counts[value] > 1:
            value = re.sub(r"[^a-z0-9_]", "_", name.replace(".", "__"))
        result[name] = value
    return result


def _profile_ds(profile: dict[str, Any] | None, name: str) -> dict[str, Any]:
    return ((profile or {}).get("datasets") or {}).get(name) or {}


def _primary_key(ds: Dataset, profile: dict[str, Any] | None, snapshot: Snapshot) -> tuple[list[str], list[dict[str, Any]], list[list[str]]]:
    """Choose the grain: declared PK > tested/proven unique single id > proven composite."""
    evidence: list[dict[str, Any]] = []
    uniques: list[list[str]] = []
    if ds.primary_key:
        evidence.append({"kind": "declared_key", "ref": f"{ds.name}({', '.join(ds.primary_key)})", "score": 1.0})
        pk = list(ds.primary_key)
    else:
        pk = []
    stems = table_stems(ds.name)
    proven_singles = [k.columns for k in key_candidates(snapshot, profile) if k.dataset == ds.name and len(k.columns) == 1
                      and (is_unique(profile, ds.name, k.columns) or k.columns in ds.unique_keys)]
    def key_rank(cols: list[str]) -> tuple[int, int]:
        col = cols[0]
        owns = any(col in {f"{s}_id", f"{s}_key", f"{s}_sk"} or col.endswith(f"{s}key") for s in stems) or col == "id"
        return (0 if owns else 1, ds.columns.index(ds.column(col)) if ds.column(col) else 99)
    proven_singles.sort(key=key_rank)
    if not pk and proven_singles:
        pk = proven_singles[0]
        origin = "dbt_test" if pk in ds.unique_keys else "uniqueness"
        evidence.append({"kind": origin, "ref": f"{ds.name}({pk[0]})", "score": 1.0})
    if not pk:
        for key in _profile_ds(profile, ds.name).get("unique_keys") or []:
            if len(key) > 1:
                pk = list(key)
                evidence.append({"kind": "uniqueness", "ref": f"{ds.name}({', '.join(pk)})", "score": 1.0})
                break
    for key in proven_singles + (_profile_ds(profile, ds.name).get("unique_keys") or []) + ds.unique_keys:
        if key != pk and key not in uniques:
            uniques.append(list(key))
    return pk, evidence, uniques


def build_entity(
    ds: Dataset,
    snapshot: Snapshot,
    profile: dict[str, Any] | None,
    ids: dict[str, str],
    outgoing: list[ScoredRelationship],
    domain: str,
) -> dict[str, Any]:
    pds = _profile_ds(profile, ds.name)
    row_count = pds.get("row_count", ds.row_count)
    pk, key_evidence, uniques = _primary_key(ds, profile, snapshot)
    col_names = [c.name for c in ds.columns]

    # Temporal structure: SCD2 history, snapshots.
    valid_from = next((c for c in _VALID_FROM if c in col_names), None)
    valid_to = next((c for c in _VALID_TO if c in col_names), None)
    current_flag = next((c for c in _CURRENT_FLAG if c in col_names), None)
    snapshot_time = None
    if len(pk) > 1:
        snapshot_time = next((c for c in pk if (ds.column(c) and ds.column(c).family in TIME_FAMILIES)), None)

    fk_cols = {c for rel in outgoing for c in rel.candidate.child_columns}
    key_cols = set(pk) | {c for key in uniques for c in key}
    attributes = [
        infer_attribute(
            col, ds,
            key_columns=key_cols,
            fk_columns=fk_cols,
            snapshot_time=snapshot_time,
            profile_col=(pds.get("columns") or {}).get(col.name) or {},
            row_count=row_count,
        )
        for col in ds.columns
    ]
    if snapshot_time:
        for attr in attributes:
            if attr.get("id") == snapshot_time or attr.get("column") == snapshot_time:
                attr["role"] = "time"
                attr.pop("semantic_type", None)

    entity: dict[str, Any] = {
        "schema": SCHEMA_ID,
        "kind": "entity",
        "id": ids[ds.name],
        "label": ids[ds.name].replace("_", " ").title(),
        "description": ds.description,
        "domain": domain,
        "entity_type": _entity_type(ds, pk, fk_cols, attributes, valid_from and valid_to, snapshot_time),
        "source": {"dataset": ds.name, **({"dbt": ds.dbt_unique_id} if ds.dbt_unique_id else {})},
    }
    if pk:
        entity["grain"] = pk
        entity["keys"] = {"primary": pk, **({"unique": uniques} if uniques else {})}

    filters: list[dict[str, Any]] = []
    if valid_from and valid_to:
        current = f"{current_flag} = TRUE" if current_flag else f"{valid_to} IS NULL"
        entity["history"] = {"type": "scd2", "valid_from": valid_from, "valid_to": valid_to, "current": current}
        filters.append({"id": "current_version", "sql": current, "reason": "scd2_current",
                        "description": "Keep only the current version of each record unless an as-of query is intended."})
    elif snapshot_time:
        entity["history"] = {"type": "snapshot", "snapshot_at": snapshot_time}
    soft_bool = next((c for c in _SOFT_DELETE_BOOL if c in col_names), None)
    soft_ts = next((c for c in _SOFT_DELETE_TS if c in col_names), None)
    if soft_bool:
        filters.append({"id": "not_deleted", "sql": f"COALESCE({soft_bool}, FALSE) = FALSE", "reason": "soft_delete",
                        "evidence": [{"kind": "heuristic", "detail": f"soft-delete flag column {soft_bool}"}]})
    elif soft_ts:
        filters.append({"id": "not_deleted", "sql": f"{soft_ts} IS NULL", "reason": "soft_delete",
                        "evidence": [{"kind": "heuristic", "detail": f"soft-delete timestamp column {soft_ts}"}]})
    if filters:
        entity["default_filters"] = filters

    entity["attributes"] = attributes
    rels = [_relationship_doc(rel, ids) for rel in sorted(outgoing, key=lambda r: (r.candidate.child_columns, r.candidate.parent))]
    if rels:
        entity["relationships"] = rels
    caveats = [c for rel in outgoing for c in rel.caveats]
    if caveats:
        entity["caveats"] = caveats
    entity["status"] = "proposed"
    entity["origin"] = "generated"
    if key_evidence:
        entity["evidence"] = key_evidence
    return entity


def _entity_type(ds: Dataset, pk: list[str], fk_cols: set[str], attributes: list[dict[str, Any]], scd2: Any, snapshot_time: str | None) -> str:
    measures = [a for a in attributes if a.get("role") == "measure"]
    if scd2:
        return "history"
    if snapshot_time:
        return "snapshot"
    if len(pk) == 2 and set(pk) <= fk_cols and len(ds.columns) - len(pk) <= 2 and not measures:
        return "bridge"
    if measures and fk_cols:
        return "fact"
    if fk_cols and any(a.get("role") == "time" for a in attributes) and not measures:
        return "event" if len(fk_cols) >= 1 and not pk else "dimension"
    return "dimension"


def _relationship_doc(rel: ScoredRelationship, ids: dict[str, str]) -> dict[str, Any]:
    cand = rel.candidate
    parent_id = ids[cand.parent]
    child_id = ids[cand.child]
    role = cand.role
    rel_id = f"{child_id}__{role}_{parent_id}" if role else f"{child_id}__{parent_id}"
    if cand.parent == cand.child and not role:
        rel_id = f"{child_id}__parent"
    doc: dict[str, Any] = {
        "id": rel_id,
        "to": parent_id,
        **({"role": role} if role else {}),
        "on": [{"from": c, "to": p} for c, p in zip(cand.child_columns, cand.parent_columns)],
        "cardinality": rel.cardinality,
    }
    if rel.optional is not None:
        doc["optional"] = bool(rel.optional)
    doc["status"] = "proposed"
    doc["origin"] = "generated"
    doc["evidence"] = rel.evidence
    return doc


def bridge_relationships(entities: dict[str, dict[str, Any]]) -> None:
    """Add many-to-many relationships between the two parents of every bridge entity."""
    for bridge in entities.values():
        if bridge.get("entity_type") != "bridge":
            continue
        rels = [r for r in bridge.get("relationships") or [] if r.get("cardinality") == "many_to_one"]
        if len(rels) != 2:
            continue
        left, right = rels[0]["to"], rels[1]["to"]
        owner = entities.get(left)
        if owner is None or left == right:
            continue
        m2m = {
            "id": f"{left}__{right}",
            "to": right,
            "cardinality": "many_to_many",
            "through": bridge["id"],
            "status": "proposed",
            "origin": "generated",
            "evidence": [{"kind": "heuristic", "detail": f"bridge entity {bridge['id']} with composite key {bridge.get('grain')}"}],
        }
        existing = owner.setdefault("relationships", [])
        if not any(r.get("id") == m2m["id"] for r in existing):
            existing.append(m2m)


# ----------------------------------------------------------------- dbt adoption
_DIM_TEMPLATE = re.compile(r"\{\{\s*(?:Dimension|TimeDimension|Entity)\(\s*['\"]([a-z0-9_]+)__([a-z0-9_]+)['\"][^)]*\)\s*\}\}")

_DBT_AGG = {"sum": "sum", "average": "avg", "avg": "avg", "min": "min", "max": "max", "count": "count",
            "count_distinct": "count_distinct", "median": "median", "sum_boolean": "sum"}


def adopt_dbt_semantics(snapshot: Snapshot, ids: dict[str, str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Turn dbt semantic models/metrics into native metrics (origin: dbt, status: approved)."""
    dbt = snapshot.semantic.get("dbt") or {}
    warnings: list[str] = []
    measures: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {}
    for sm in dbt.get("semantic_models") or []:
        ds = snapshot.get(sm.get("dataset") or "") if sm.get("dataset") else None
        if ds is None:
            warnings.append(f"dbt semantic model {sm.get('name')}: dataset {sm.get('dataset')} not in snapshot")
            continue
        for measure in sm.get("measures") or []:
            measures[measure.get("name")] = (ids[ds.name], measure, sm)
    metrics: list[dict[str, Any]] = []
    for metric in dbt.get("metrics") or []:
        mtype = metric.get("type")
        doc: dict[str, Any] = {"id": _ident(metric.get("name", "")), "label": metric.get("label"),
                               "description": metric.get("description")}
        if mtype == "simple":
            entry = measures.get(metric.get("measure"))
            if entry is None:
                warnings.append(f"dbt metric {metric.get('name')}: measure {metric.get('measure')} not found")
                continue
            entity_id, measure, sm = entry
            doc.update({"type": "simple", "entity": entity_id, "agg": _DBT_AGG.get(measure.get("agg"), "sum"),
                        "expr": str(measure.get("expr") or measure.get("name"))})
            agg_time = measure.get("agg_time_dimension") or (sm.get("defaults") or {}).get("agg_time_dimension")
            if agg_time:
                dim = next((d for d in sm.get("dimensions") or [] if d.get("name") == agg_time), None)
                doc["time"] = _ident(str((dim or {}).get("expr") or agg_time))
        elif mtype == "ratio":
            doc.update({"type": "ratio", "numerator": _ident(metric.get("numerator") or ""), "denominator": _ident(metric.get("denominator") or "")})
        elif mtype == "derived":
            doc.update({"type": "derived", "expr": metric.get("expr"), "inputs": [_ident(i) for i in metric.get("inputs") or [] if i]})
        else:
            warnings.append(f"dbt metric {metric.get('name')}: type {mtype} kept in dbt only (not modelled natively)")
            continue
        if metric.get("filter"):
            doc["filter"] = _DIM_TEMPLATE.sub(lambda m: m.group(2), str(metric["filter"]))
        doc.update({"status": "approved", "origin": "dbt", "evidence": [{"kind": "dbt_semantic", "ref": f"metric.{metric.get('name')}"}]})
        metrics.append({k: v for k, v in doc.items() if v not in (None, "", [])})
    return metrics, warnings


def _ident(text: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", str(text).lower())


def suggested_metrics(entities: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Draft metric suggestions (written to _proposals/, never activated automatically)."""
    out: list[dict[str, Any]] = []
    for entity in entities.values():
        if entity.get("entity_type") not in {"fact", "event", "snapshot"}:
            continue
        time_attr = next((a["id"] for a in entity.get("attributes", []) if a.get("role") == "time"), None)
        out.append({"id": f"{entity['id']}_count", "type": "simple", "entity": entity["id"], "agg": "count",
                    "expr": "*", **({"time": time_attr} if time_attr else {}), "status": "draft", "origin": "generated",
                    "rationale": "row count of the entity grain; confirm the business name before approving"})
        for attr in entity.get("attributes", []):
            if attr.get("role") != "measure" or attr.get("additivity") != "additive":
                continue
            expr = attr.get("column") or attr["id"]
            if attr.get("scale"):
                expr = f"{expr} * {attr['scale']}"
            metric = {"id": f"total_{attr['id']}".replace("_cents", ""), "type": "simple", "entity": entity["id"],
                      "agg": attr.get("aggregation", "sum"), "expr": expr, **({"time": time_attr} if time_attr else {}),
                      "status": "draft", "origin": "generated",
                      "rationale": "additive measure; decide gross vs net, required filters and currency handling before approving"}
            if (attr.get("money") or {}).get("currency_attr"):
                metric["rationale"] += f"; amounts are in mixed currencies ({attr['money']['currency_attr']})"
            out.append(metric)
    return out
