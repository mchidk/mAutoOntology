"""Export to a Snowflake semantic view YAML (Cortex Analyst semantic model format).

Entities become logical tables (dimensions, time_dimensions, facts, metrics,
filters), relationships become ``relationships`` with column pairs, ratio and
derived metrics become view-level metrics, verified queries are carried over,
and caveats/default filters become SQL-generation instructions.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..context import metric_sql
from ..model import Project
from ..yamlio import ordered, write_file
from .common import (
    active,
    attr_expr,
    measure_expr,
    ordered_entities,
    snowflake_type,
    split_dataset,
)

_NAME_SAFE = str.maketrans({"-": "_", " ": "_"})


def build(project: Project) -> tuple[dict[str, Any], list[str]]:
    notes: list[str] = []
    name = ((project.config.get("project") or {}).get("name") or project.root.name).translate(_NAME_SAFE)
    default_db = (project.config.get("conventions") or {}).get("snowflake_database")
    tables = []
    instructions: list[str] = []
    for entity in ordered_entities(project):
        raw = entity.raw
        database, schema, table = split_dataset(entity.dataset)
        database = database or default_db
        base = {k: v for k, v in (("database", database), ("schema", schema), ("table", table)) if v}
        if not database:
            notes.append(f"{entity.id}: no database in {entity.dataset}; set conventions.snowflake_database in mautoontology.yaml")
        out: dict[str, Any] = {"name": entity.id}
        if raw.get("description"):
            out["description"] = raw["description"]
        out["base_table"] = {k: str(v).upper() for k, v in base.items()}
        if raw.get("grain"):
            out["primary_key"] = {"columns": list(raw["grain"])}
        dims, times, facts = [], [], []
        for attr in entity.attributes:
            if not active(attr):
                continue
            item: dict[str, Any] = {"name": attr["id"], "expr": attr_expr(attr), "data_type": snowflake_type(attr.get("family"))}
            if attr.get("synonyms"):
                item["synonyms"] = list(attr["synonyms"])
            if attr.get("description"):
                item["description"] = attr["description"]
            role = attr.get("role")
            if role == "time":
                times.append(item)
            elif role == "measure":
                item["expr"] = measure_expr(attr)
                facts.append(item)
            else:
                if attr.get("values") and attr.get("pii") in (None, "none"):
                    item["sample_values"] = [str(v) for v in attr["values"][:25]]
                    if attr.get("values_complete"):
                        item["is_enum"] = True
                dims.append(item)
        for key, values in (("dimensions", dims), ("time_dimensions", times), ("facts", facts)):
            if values:
                out[key] = values
        table_metrics = []
        for metric in sorted(project.metrics.values(), key=lambda m: m["id"]):
            if metric.get("type") != "simple" or metric.get("entity") != entity.id or not active(metric):
                continue
            item = {"name": metric["id"], "expr": metric_sql(project, metric)}
            if metric.get("synonyms"):
                item["synonyms"] = list(metric["synonyms"])
            if metric.get("description"):
                item["description"] = metric["description"]
            for column in [metric.get("expr")]:
                attr = entity.attribute_for_column(str(column or ""))
                if attr and attr.get("additivity") == "semi_additive" and attr.get("non_additive_over"):
                    item["non_additive_dimensions"] = [{"table": entity.id, "dimension": attr["non_additive_over"],
                                                        "sort_direction": "descending"}]
            table_metrics.append(item)
        if table_metrics:
            out["metrics"] = table_metrics
        filters = [{"name": f["id"], "expr": f["sql"], "description": f.get("description") or f["reason"].replace("_", " ")}
                   for f in raw.get("default_filters") or []]
        filters += [{"name": s["id"], "expr": s["sql"], **({"description": s["description"]} if s.get("description") else {})}
                    for s in raw.get("segments") or []]
        if filters:
            out["filters"] = filters
        for flt in raw.get("default_filters") or []:
            instructions.append(f"Always apply {entity.id} filter {flt['id']} ({flt['sql']}) unless the question asks otherwise.")
        for caveat in raw.get("caveats") or []:
            instructions.append(f"{entity.id}: {caveat}")
        tables.append(out)

    relationships = []
    for rel in sorted(project.relationships(), key=lambda r: r.id):
        if rel.cardinality == "many_to_many":
            continue
        item = {"name": rel.id, "left_table": rel.from_entity, "right_table": rel.to_entity,
                "relationship_columns": [{"left_column": c, "right_column": p} for c, p in rel.pairs]}
        relationships.append(item)

    view_metrics = []
    for metric in sorted(project.metrics.values(), key=lambda m: m["id"]):
        if metric.get("type") not in {"ratio", "derived"} or not active(metric):
            continue
        if metric["type"] == "ratio":
            num, den = project.metrics.get(metric["numerator"], {}), project.metrics.get(metric["denominator"], {})
            expr = f"{num.get('entity')}.{metric['numerator']} / NULLIF({den.get('entity')}.{metric['denominator']}, 0)"
        else:
            owners = {ref: project.metrics.get(ref, {}).get("entity") for ref in metric.get("inputs") or []}
            words = "|".join(re.escape(name) for name in sorted(owners, key=len, reverse=True))
            pattern = re.compile(rf"\b({words})\b") if owners else None
            expr = str(metric.get("expr"))
            if pattern is not None:
                expr = pattern.sub(lambda m, owners=owners: f"{owners[m.group(1)]}.{m.group(1)}" if owners[m.group(1)] else m.group(1), expr)
        item = {"name": metric["id"], "expr": expr}
        if metric.get("description"):
            item["description"] = metric["description"]
        view_metrics.append(item)

    verified = []
    for query in sorted(project.queries.values(), key=lambda q: q["id"]):
        item: dict[str, Any] = {"name": query["id"], "question": query["question"], "sql": query["sql"]}
        if query.get("verified_by"):
            item["verified_by"] = query["verified_by"]
        if query.get("verified_at"):
            item["verified_at"] = _epoch(query["verified_at"])
        item["use_as_onboarding_question"] = False
        verified.append(item)

    doc: dict[str, Any] = {"name": name}
    description = (project.config.get("project") or {}).get("description")
    if description:
        doc["description"] = description
    doc["tables"] = tables
    if relationships:
        doc["relationships"] = relationships
    if view_metrics:
        doc["metrics"] = view_metrics
    if verified:
        doc["verified_queries"] = verified
    if instructions:
        doc["module_custom_instructions"] = {"sql_generation": " ".join(instructions)}
    return doc, notes


def _epoch(value: Any) -> int:
    if isinstance(value, int):
        return value
    try:
        return int(datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return 0


def write(project: Project, out: Path) -> list[Path]:
    doc, notes = build(project)
    paths = [write_file(out / "snowflake_semantic_view.yaml", ordered(doc))]
    if notes:
        notes_path = out / "snowflake_export_notes.md"
        notes_path.write_bytes(("# Snowflake export notes\n\n" + "\n".join(f"- {n}" for n in notes) + "\n").encode("utf-8"))
        paths.append(notes_path)
    return paths
