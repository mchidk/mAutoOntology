"""Export human-readable documentation: a data dictionary plus a Mermaid ER diagram."""

from __future__ import annotations

from pathlib import Path

from ..context import metric_sql
from ..model import Project
from .common import active, ordered_entities

_MERMAID_CARD = {"many_to_one": "}o--||", "one_to_one": "|o--||", "many_to_many": "}o--o{"}


def build(project: Project) -> str:
    name = (project.config.get("project") or {}).get("name") or project.root.name
    lines = [f"# {name}: semantic layer", ""]
    description = (project.config.get("project") or {}).get("description")
    if description:
        lines += [description, ""]
    lines += ["## Entity-relationship diagram", "", "```mermaid", "erDiagram"]
    for rel in sorted(project.relationships(), key=lambda r: r.id):
        label = rel.role or rel.id.split("__")[-1]
        lines.append(f"    {rel.from_entity} {_MERMAID_CARD.get(rel.cardinality, '}o--||')} {rel.to_entity} : \"{label}\"")
    for entity in ordered_entities(project):
        lines.append(f"    {entity.id} {{")
        for attr in entity.attributes[:25]:
            marker = " PK" if attr.get("column", attr["id"]) in (entity.raw.get("grain") or []) else ""
            lines.append(f"        {attr.get('family', 'other')} {attr['id']}{marker}")
        lines.append("    }")
    lines += ["```", ""]
    for entity in ordered_entities(project):
        raw = entity.raw
        lines += [f"## {raw.get('label') or entity.id} (`{entity.id}`)", ""]
        lines.append(f"- **Dataset:** `{entity.dataset}`  ")
        lines.append(f"- **Type:** {raw.get('entity_type', 'entity')} | **Status:** {raw.get('status', 'proposed')}  ")
        lines.append(f"- **Grain:** {', '.join(raw.get('grain') or ['unknown'])}")
        if raw.get("description"):
            lines += ["", raw["description"]]
        for flt in raw.get("default_filters") or []:
            lines.append(f"- **Default filter `{flt['id']}`:** `{flt['sql']}` ({flt['reason'].replace('_', ' ')})")
        lines += ["", "| Attribute | Role | Type | Notes |", "| :--- | :--- | :--- | :--- |"]
        for attr in entity.attributes:
            if not active(attr):
                continue
            notes = []
            for key in ("semantic_type", "aggregation", "additivity"):
                if attr.get(key):
                    notes.append(f"{key.replace('_', ' ')}: {attr[key]}")
            if attr.get("pii") not in (None, "none"):
                notes.append(f"PII: {attr['pii']}")
            if attr.get("values"):
                notes.append("values: " + ", ".join(map(str, attr["values"][:8])))
            if attr.get("description"):
                notes.append(attr["description"])
            lines.append(f"| `{attr['id']}` | {attr.get('role', '')} | {attr.get('family', '')} | {'; '.join(notes)} |")
        rels = [r for r in project.relationships() if r.from_entity == entity.id]
        if rels:
            lines += ["", "**Relationships**", ""]
            for rel in rels:
                on = " AND ".join(f"{c} = {rel.to_entity}.{p}" for c, p in rel.pairs) or f"through {rel.through}"
                lines.append(f"- `{rel.id}` -> `{rel.to_entity}` ({rel.cardinality.replace('_', ' ')}, {rel.status}): {on}")
        lines.append("")
    metrics = [m for m in sorted(project.metrics.values(), key=lambda m: m["id"]) if active(m)]
    if metrics:
        lines += ["## Metrics", "", "| Metric | Definition | Status | Notes |", "| :--- | :--- | :--- | :--- |"]
        for metric in metrics:
            sql = (metric_sql(project, metric) or "").replace("|", "\\|")
            notes = metric.get("description") or ""
            if metric.get("disambiguation"):
                notes += f" ({metric['disambiguation']})"
            lines.append(f"| `{metric['id']}` | `{sql}` | {metric.get('status', 'proposed')} | {notes} |")
        lines.append("")
    if project.terms:
        lines += ["## Glossary", ""]
        for term_id in sorted(project.terms):
            term = project.terms[term_id]
            lines.append(f"- **{term_id}**: {term['definition']}")
        lines.append("")
    return "\n".join(lines)


def write(project: Project, out: Path) -> list[Path]:
    path = out / "SEMANTIC_LAYER.md"
    path.write_bytes(build(project).encode("utf-8"))
    return [path]
