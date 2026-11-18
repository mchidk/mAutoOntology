"""Export to Databricks Unity Catalog metric views (YAML spec 1.1).

One metric view per fact/event/snapshot entity: the entity is the ``source``,
to-one relationships reachable from it become (nested) ``joins`` (only to-one
joins, so measures can never fan out), dimensions of the fact and its joined
dimensions become ``fields``, metrics on the entity become ``measures`` and the
entity's default filters become the view ``filter``. A ready-to-run
``CREATE OR REPLACE VIEW ... WITH METRICS LANGUAGE YAML`` statement is written
next to each YAML file.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..model import Project
from ..yamlio import dump_text, ordered, write_file
from .common import active, attr_expr, ordered_entities, unqualified_metric_sql

SPEC_VERSION = "1.1"
_MAX_DEPTH = 2




def _joins(project: Project, entity_id: str, alias: str, depth: int, seen: set[str]) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    joins, reachable = [], []
    if depth > _MAX_DEPTH:
        return joins, reachable
    for rel in sorted(project.relationships(), key=lambda r: r.id):
        if rel.from_entity != entity_id or rel.cardinality not in {"many_to_one", "one_to_one"} or rel.to_entity in seen:
            continue
        parent = project.entities.get(rel.to_entity)
        if parent is None:
            continue
        base = f"{rel.role}_{rel.to_entity}" if rel.role else rel.to_entity
        name = base if alias == "source" else f"{alias}_{base}"  # join names must be unique across nesting
        on = " AND ".join(f"{alias}.{c} = {name}.{p}" for c, p in rel.pairs)
        join: dict[str, Any] = {"name": name, "source": parent.dataset, "on": on}
        if rel.cardinality == "many_to_one":
            join["cardinality"] = "many_to_one"
        nested, nested_reach = _joins(project, rel.to_entity, name, depth + 1, seen | {rel.to_entity})
        if nested:
            join["joins"] = nested
        joins.append(join)
        reachable.append((name, rel.to_entity))
        reachable.extend(nested_reach)
    return joins, reachable


def build_view(project: Project, entity_id: str) -> dict[str, Any]:
    entity = project.entities[entity_id]
    raw = entity.raw
    doc: dict[str, Any] = {"version": SPEC_VERSION, "source": entity.dataset}
    if raw.get("description"):
        doc["comment"] = raw["description"]
    filters = [f["sql"] for f in raw.get("default_filters") or []]
    if filters:
        doc["filter"] = " AND ".join(f"({f})" for f in filters)
    joins, reachable = _joins(project, entity_id, "source", 1, {entity_id})
    if joins:
        doc["joins"] = joins
    fields = []
    for attr in entity.attributes:
        if active(attr) and attr.get("role") in {"dimension", "time"} and attr.get("pii") in (None, "none"):
            fields.append(_field(attr["id"], attr_expr(attr), attr))
    for alias, joined_id in reachable:
        joined = project.entities[joined_id]
        for attr in joined.attributes:
            if active(attr) and attr.get("role") in {"dimension", "time"} and attr.get("pii") in (None, "none"):
                fields.append(_field(f"{alias}_{attr['id']}", f"{alias}.{attr_expr(attr)}", attr))
    if fields:
        doc["fields"] = fields
    measures = []
    for metric in sorted(project.metrics.values(), key=lambda m: m["id"]):
        if metric.get("type") == "simple" and metric.get("entity") == entity_id and active(metric):
            item: dict[str, Any] = {"name": metric["id"], "expr": unqualified_metric_sql(project, metric)}
            if metric.get("description"):
                item["comment"] = metric["description"]
            if metric.get("synonyms"):
                item["synonyms"] = list(metric["synonyms"])[:10]
            measures.append(item)
    for metric in sorted(project.metrics.values(), key=lambda m: m["id"]):
        if metric.get("type") == "ratio" and active(metric):
            num, den = project.metrics.get(metric["numerator"], {}), project.metrics.get(metric["denominator"], {})
            if num.get("entity") == entity_id and den.get("entity") == entity_id:
                measures.append({"name": metric["id"], "expr": f"MEASURE({metric['numerator']}) / NULLIF(MEASURE({metric['denominator']}), 0)",
                                 **({"comment": metric["description"]} if metric.get("description") else {})})
    if measures:
        doc["measures"] = measures
    return doc


def _field(name: str, expr: str, attr: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {"name": name, "expr": expr}
    if attr.get("description"):
        item["comment"] = attr["description"]
    if attr.get("label"):
        item["display_name"] = attr["label"][:255]
    if attr.get("synonyms"):
        item["synonyms"] = list(attr["synonyms"])[:10]
    return item


def _quote_on_keys(node: Any) -> Any:
    """YAML 1.1 parsers read a bare ``on`` key as boolean true; Databricks documents it quoted."""
    from ruamel.yaml.comments import CommentedMap
    from ruamel.yaml.scalarstring import SingleQuotedScalarString

    if isinstance(node, CommentedMap):
        for key in list(node.keys()):
            value = _quote_on_keys(node[key])
            if key == "on":
                position = list(node.keys()).index(key)
                del node[key]
                node.insert(position, SingleQuotedScalarString("on"), value)
            else:
                node[key] = value
    elif isinstance(node, list):
        for index, item in enumerate(node):
            node[index] = _quote_on_keys(item)
    return node


def write(project: Project, out: Path) -> list[Path]:
    paths: list[Path] = []
    notes: list[str] = []
    targets = [e for e in ordered_entities(project) if e.raw.get("entity_type") in {"fact", "event", "snapshot"}]
    for entity in targets:
        doc = build_view(project, entity.id)
        if "measures" not in doc and "fields" not in doc:
            continue
        rendered = _quote_on_keys(ordered(doc))
        paths.append(write_file(out / f"{entity.id}_metric_view.yaml", rendered))
        body = dump_text(rendered)
        if "$$" in body:  # text containing $$ would end the dollar-quoted body and run as SQL
            notes.append(f"{entity.id}: no .sql written because the YAML contains '$$'; remove it from descriptions or create the view manually")
            continue
        view_name = re.sub(r"[^a-z0-9_.]", "_", f"{entity.dataset}_metrics".lower())
        sql = f"CREATE OR REPLACE VIEW {view_name}\nWITH METRICS\nLANGUAGE YAML\nAS $$\n{body}$$;\n"
        sql_path = out / f"{entity.id}_metric_view.sql"
        sql_path.write_bytes(sql.encode("utf-8"))
        paths.append(sql_path)
    if notes:
        notes_path = out / "databricks_export_notes.md"
        notes_path.write_bytes(("# Databricks export notes\n\n" + "\n".join(f"- {n}" for n in notes) + "\n").encode("utf-8"))
        paths.append(notes_path)
    return paths
