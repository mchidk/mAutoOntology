"""Export to dbt Semantic Layer (MetricFlow) YAML: ``semantic_models`` + ``metrics``.

Uses the widely deployed ``semantic_models`` / ``measures`` format (dbt-core
1.6+). Metrics adopted from dbt (``origin: dbt``) are not exported again, so
dbt stays the single source of truth for what it already defines.

Limits that are reported, not hidden: MetricFlow entities are single-column, so
composite keys are skipped; role-playing foreign keys get role-prefixed entity
names (MetricFlow joins on entity names, so these need a matching entity in the
parent to be joinable).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from ..model import Entity, Project
from ..yamlio import ordered, write_file
from .common import active, attr_expr, measure_expr, ordered_entities

_AGG = {"sum": "sum", "avg": "average", "min": "min", "max": "max", "count": "count",
        "count_distinct": "count_distinct", "median": "median"}


def _model_ref(entity: Entity) -> str:
    dbt_id = (entity.raw.get("source") or {}).get("dbt")
    name = dbt_id.split(".")[-1] if dbt_id else entity.dataset.split(".")[-1]
    return f"ref('{name}')"


def _filter_template(sql: str, entity_id: str) -> str:
    tree = sqlglot.parse_one(sql)

    def to_dimension(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column):
            return exp.Var(this=f"{{{{ Dimension('{entity_id}__{node.name}') }}}}")
        return node

    return tree.transform(to_dimension).sql()


def build(project: Project) -> tuple[dict[str, Any], list[str]]:
    notes: list[str] = []
    skipped: set[str] = set()
    semantic_models = []
    measure_for_metric: dict[str, str] = {}
    for entity in ordered_entities(project):
        raw = entity.raw
        grain = raw.get("grain") or []
        entities_out: list[dict[str, Any]] = []
        if len(grain) == 1:
            entities_out.append({"name": entity.id, "type": "primary", "expr": grain[0]})
        elif grain:
            notes.append(f"{entity.id}: composite grain {grain}; exported with primary_entity (MetricFlow entities are single-column)")
        for rel in project.relationships():
            if rel.from_entity != entity.id or rel.cardinality == "many_to_many" or len(rel.pairs) != 1:
                continue
            name = f"{rel.role}_{rel.to_entity}" if rel.role else rel.to_entity
            if name == entity.id:
                name = f"parent_{entity.id}"
            if any(e["name"] == name for e in entities_out):
                continue
            entities_out.append({"name": name, "type": "foreign", "expr": rel.pairs[0][0]})
            if rel.role:
                notes.append(f"{entity.id}.{rel.pairs[0][0]}: role-playing entity '{name}' (add a matching entity to join through it)")
        dimensions, measures = [], []
        time_attrs = []
        key_cols = {e["expr"] for e in entities_out}
        for attr in entity.attributes:
            if not active(attr):
                continue
            role = attr.get("role")
            if role == "time":
                grain_name = (attr.get("time") or {}).get("grains", ["day"])
                granularity = "day" if "day" in grain_name else grain_name[0]
                dimensions.append({"name": attr["id"], "type": "time", **({"expr": attr_expr(attr)} if attr_expr(attr) != attr["id"] else {}),
                                   "type_params": {"time_granularity": granularity}})
                time_attrs.append(attr["id"])
            elif role in {"dimension"} and attr_expr(attr) not in key_cols:
                dimensions.append({"name": attr["id"], "type": "categorical", **({"expr": attr_expr(attr)} if attr_expr(attr) != attr["id"] else {}),
                                   **({"description": attr["description"]} if attr.get("description") else {})})
            elif role == "measure" and attr.get("aggregation") in _AGG:
                measure = {"name": attr["id"], "agg": _AGG[attr["aggregation"]], "expr": measure_expr(attr)}
                if attr.get("additivity") == "semi_additive" and attr.get("non_additive_over"):
                    measure["non_additive_dimension"] = {"name": attr["non_additive_over"], "window_choice": "max"}
                if attr.get("description"):
                    measure["description"] = attr["description"]
                measures.append(measure)
        for metric in sorted(project.metrics.values(), key=lambda m: m["id"]):
            if metric.get("type") != "simple" or metric.get("entity") != entity.id or not active(metric) or metric.get("origin") == "dbt":
                continue
            if not time_attrs:
                skipped.add(metric["id"])
                notes.append(f"metric {metric['id']}: skipped, entity {entity.id} has no time dimension (MetricFlow needs an aggregation time)")
                continue
            expr = str(metric.get("expr") or "*")
            agg = metric.get("agg", "sum")
            measure_name = f"{metric['id']}_measure"
            measure: dict[str, Any] = {"name": measure_name, "agg": _AGG.get(agg, "sum"),
                                       "expr": "1" if expr == "*" else expr}
            if metric.get("time"):
                measure["agg_time_dimension"] = metric["time"]
            measures.append(measure)
            measure_for_metric[metric["id"]] = measure_name
        model: dict[str, Any] = {"name": entity.id, "model": _model_ref(entity)}
        if raw.get("description"):
            model["description"] = raw["description"]
        if time_attrs:
            model["defaults"] = {"agg_time_dimension": time_attrs[0]}
        elif measures:
            notes.append(f"{entity.id}: measures {[m['name'] for m in measures]} skipped (no time dimension for MetricFlow aggregation)")
            measures = []
        if len(grain) > 1 and dimensions:
            model["primary_entity"] = entity.id
        if entities_out:
            model["entities"] = entities_out
        if dimensions:
            model["dimensions"] = dimensions
        if measures:
            model["measures"] = measures
        semantic_models.append(model)

    metrics_out = []
    changed = True
    while changed:  # a ratio/derived metric over a skipped metric is skipped too
        changed = False
        for metric in project.metrics.values():
            deps = list(metric.get("inputs") or []) + [m for m in (metric.get("numerator"), metric.get("denominator")) if m]
            if metric["id"] not in skipped and any(d in skipped for d in deps):
                skipped.add(metric["id"])
                notes.append(f"metric {metric['id']}: skipped, depends on a skipped metric")
                changed = True
    for metric in sorted(project.metrics.values(), key=lambda m: m["id"]):
        if not active(metric) or metric.get("origin") == "dbt" or metric["id"] in skipped:
            continue
        item: dict[str, Any] = {"name": metric["id"], "label": metric.get("label") or metric["id"].replace("_", " ").title()}
        if metric.get("description"):
            item["description"] = metric["description"]
        mtype = metric.get("type")
        if mtype == "simple":
            item["type"] = "simple"
            item["type_params"] = {"measure": measure_for_metric.get(metric["id"], f"{metric['id']}_measure")}
            if metric.get("filter"):
                item["filter"] = _filter_template(metric["filter"], metric["entity"])
        elif mtype == "ratio":
            item["type"] = "ratio"
            item["type_params"] = {"numerator": {"name": metric["numerator"]}, "denominator": {"name": metric["denominator"]}}
        elif mtype == "derived":
            item["type"] = "derived"
            item["type_params"] = {"expr": metric["expr"], "metrics": [{"name": m} for m in metric.get("inputs") or []]}
        metrics_out.append(item)
    doc: dict[str, Any] = {"semantic_models": semantic_models}
    if metrics_out:
        doc["metrics"] = metrics_out
    return doc, notes


def write(project: Project, out: Path) -> list[Path]:
    doc, notes = build(project)
    paths = [write_file(out / "semantic_models.yml", ordered(doc))]
    if notes:
        notes_path = out / "dbt_export_notes.md"
        notes_path.write_bytes(("# dbt export notes\n\n" + "\n".join(f"- {n}" for n in notes) + "\n").encode("utf-8"))
        paths.append(notes_path)
    return paths
