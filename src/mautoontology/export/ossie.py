"""Export to Apache Ossie (incubating), core spec 0.3.0.dev0.

Portable structure (datasets, fields, relationships, metrics, ai_context) maps
directly. Everything Ossie's core does not model - governance status, evidence,
additivity, default filters, SCD2 history, PII classes - is carried losslessly
in ``nonportable_extensions`` under the vendor name ``mautoontology``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..model import Project, load_schema
from ..yamlio import ordered, write_file
from .common import (
    active,
    attr_expr,
    field_name_for_column,
    governance,
    ordered_entities,
    ossie_type,
    unqualified_metric_sql,
)

OSSIE_VERSION = "0.3.0.dev0"
VENDOR = "mautoontology"


def _ext(data: dict[str, Any]) -> list[dict[str, Any]] | None:
    data = {k: v for k, v in data.items() if v not in (None, [], {}, "")}
    return [{"vendor_name": VENDOR, "data": data}] if data else None


def _ai_context(element: dict[str, Any], extra: str | None = None) -> dict[str, Any] | None:
    ctx: dict[str, Any] = {}
    if element.get("synonyms"):
        ctx["synonyms"] = list(element["synonyms"])
    notes = " ".join(filter(None, [element.get("disambiguation"), " ".join(element.get("caveats") or []), extra]))
    if notes:
        ctx["instructions"] = notes
    return ctx or None


def build(project: Project) -> dict[str, Any]:
    name = (project.config.get("project") or {}).get("name") or project.root.name
    datasets = []
    for entity in ordered_entities(project):
        raw = entity.raw
        fields = []
        for attr in entity.attributes:
            if not active(attr):
                continue
            field: dict[str, Any] = {"name": attr["id"], "expression": {"sql": attr_expr(attr)}}
            if attr.get("role") in {"dimension", "time", "identifier"}:
                field["dimension"] = {"is_time": attr.get("role") == "time"}
            field["datatype"] = ossie_type(attr.get("family"))
            if attr.get("label"):
                field["label"] = attr["label"]
            if attr.get("description"):
                field["description"] = attr["description"]
            ctx = _ai_context(attr)
            if ctx:
                field["ai_context"] = ctx
            ext = _ext({
                "role": attr.get("role"), "semantic_type": attr.get("semantic_type"), "aggregation": attr.get("aggregation"),
                "additivity": attr.get("additivity"), "non_additive_over": attr.get("non_additive_over"), "money": attr.get("money"),
                "scale": attr.get("scale"), "pii": attr.get("pii"), "values": attr.get("values") if attr.get("pii") in (None, "none") else None,
                **governance(attr),
            })
            if ext:
                field["nonportable_extensions"] = ext
            fields.append(field)
        dataset: dict[str, Any] = {"name": entity.id, "source": entity.dataset}
        pk = [field_name_for_column(entity, c) for c in raw.get("grain") or []]
        if pk:
            dataset["primary_key"] = pk
        uniques = [[field_name_for_column(entity, c) for c in key] for key in (raw.get("keys") or {}).get("unique") or []]
        if uniques:
            dataset["unique_keys"] = uniques
        if raw.get("description"):
            dataset["description"] = raw["description"]
        filters = "; ".join(f"default filter {f['id']}: {f['sql']} ({f['reason']})" for f in raw.get("default_filters") or [])
        ctx = _ai_context(raw, filters or None)
        if ctx:
            dataset["ai_context"] = ctx
        dataset["fields"] = fields
        scoped = []
        for metric in sorted(project.metrics.values(), key=lambda m: m["id"]):
            if metric.get("type") == "simple" and metric.get("entity") == entity.id and active(metric):
                scoped.append(_metric(project, metric, unqualified=True))
        if scoped:
            dataset["metrics"] = scoped
        ext = _ext({"entity_type": raw.get("entity_type"), "history": raw.get("history"), "default_filters": raw.get("default_filters"),
                    "segments": raw.get("segments"), **governance(raw)})
        if ext:
            dataset["nonportable_extensions"] = ext
        datasets.append(dataset)

    relationships = []
    for rel in sorted(project.relationships(), key=lambda r: r.id):
        if rel.cardinality == "many_to_many":
            continue
        child, parent = project.entities[rel.from_entity], project.entities[rel.to_entity]
        item: dict[str, Any] = {
            "name": rel.id,
            "from": rel.from_entity,
            "to": rel.to_entity,
            "from_columns": [field_name_for_column(child, c) for c, _ in rel.pairs],
            "to_columns": [field_name_for_column(parent, p) for _, p in rel.pairs],
        }
        ext = _ext({"cardinality": rel.cardinality, "role": rel.role, "optional": rel.optional, "as_of": rel.as_of,
                    "condition": rel.condition, **governance(rel.raw)})
        if ext:
            item["nonportable_extensions"] = ext
        relationships.append(item)

    model_metrics = [_metric(project, m, unqualified=False) for m in sorted(project.metrics.values(), key=lambda m: m["id"])
                     if m.get("type") in {"ratio", "derived"} and active(m)]
    doc: dict[str, Any] = {"version": OSSIE_VERSION, "name": name}
    description = (project.config.get("project") or {}).get("description")
    if description:
        doc["description"] = description
    doc["datasets"] = datasets
    if relationships:
        doc["relationships"] = relationships
    if model_metrics:
        doc["metrics"] = model_metrics
    return doc


def _metric(project: Project, metric: dict[str, Any], unqualified: bool) -> dict[str, Any]:
    from ..context import metric_sql

    sql = unqualified_metric_sql(project, metric) if unqualified else metric_sql(project, metric)
    item: dict[str, Any] = {"name": metric["id"], "expression": {"sql": sql or "NULL"}}
    if metric.get("description"):
        item["description"] = metric["description"]
    ctx = _ai_context(metric)
    if ctx:
        item["ai_context"] = ctx
    ext = _ext({"type": metric.get("type"), "agg": metric.get("agg"), "expr": metric.get("expr"), "filter": metric.get("filter"),
                "time": metric.get("time"), "grains": metric.get("grains"), "unit": metric.get("unit"), **governance(metric)})
    if ext:
        item["nonportable_extensions"] = ext
    return item


def validate_document(doc: dict[str, Any]) -> list[str]:
    """Validate against the vendored Apache Ossie JSON schema; returns error messages."""
    from jsonschema import Draft202012Validator

    schema = load_schema("ossie-0.3.0.dev0.schema.json")
    clean = json.loads(json.dumps(doc, default=str))
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in Draft202012Validator(schema).iter_errors(clean)]


def write(project: Project, out: Path) -> list[Path]:
    doc = build(project)
    errors = validate_document(doc)
    if errors:
        raise ValueError("Ossie export failed schema validation:\n" + "\n".join(errors[:20]))
    path = write_file(out / "ossie.yaml", ordered(doc))
    return [path]
