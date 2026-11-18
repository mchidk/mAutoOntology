"""Build compact, prompt-ready context for a question: the minimal connecting subgraph.

The host agent reads ``semantics/INDEX.md``, picks the entities/metrics/terms the
question needs, and calls ``context`` with them. When it cannot pick, a lexical
search over ids, labels, synonyms and descriptions suggests elements. The output
is everything needed to write correct SQL and nothing else: entity grains and
keys, the attributes that matter, metric definitions, the join path rendered as
SQL, default filters, warnings (fan-out, chasm, role-playing, SCD2, currency),
matching verified queries and caveats.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from .graph import JoinGraph, default_filter_sql, render_from_clause
from .model import Project
from .sqlutil import entity_alias

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "per", "by", "for", "in", "on", "and", "or", "to", "what", "which", "how", "many",
         "much", "is", "are", "was", "were", "show", "me", "list", "give", "with", "each", "all", "total", "number"}


def _tokens(text: str) -> list[str]:
    out = []
    for token in _TOKEN.findall(text.lower()):
        if token in _STOP:
            continue
        out.append(token)
        if token.endswith("s") and len(token) > 3:
            out.append(token[:-1])
    return out


def search(project: Project, question: str, limit: int = 8) -> list[dict[str, Any]]:
    """Rank entities, metrics, glossary terms and attributes against a question (BM25-style)."""
    docs: list[tuple[str, str, str]] = []  # (kind, id, text)
    for entity in project.entities.values():
        raw = entity.raw
        docs.append(("entity", entity.id, " ".join(filter(None, [entity.id.replace("_", " "), raw.get("label"), raw.get("description"),
                                                               " ".join(raw.get("synonyms") or []), entity.dataset.split(".")[-1]]))))
        for attr in entity.attributes:
            docs.append(("attribute", f"{entity.id}.{attr['id']}", " ".join(filter(None, [
                attr["id"].replace("_", " "), attr.get("label"), attr.get("description"), " ".join(attr.get("synonyms") or [])]))))
    for metric_id, metric in project.metrics.items():
        docs.append(("metric", metric_id, " ".join(filter(None, [metric_id.replace("_", " "), metric.get("label"), metric.get("description"),
                                                               " ".join(metric.get("synonyms") or [])]))))
    for term_id, term in project.terms.items():
        docs.append(("term", term_id, " ".join(filter(None, [term_id.replace("_", " "), term.get("label"), term.get("definition"),
                                                           " ".join(term.get("synonyms") or [])]))))
    query = _tokens(question)
    if not query or not docs:
        return []
    tokenized = [Counter(_tokens(text)) for _, _, text in docs]
    avg_len = sum(sum(t.values()) for t in tokenized) / len(tokenized) or 1.0
    df = Counter()
    for tokens in tokenized:
        df.update(set(tokens))
    n = len(docs)
    scored = []
    for (kind, ident, _), tokens in zip(docs, tokenized):
        length = sum(tokens.values()) or 1
        score = 0.0
        for term in set(query):
            if term not in tokens:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            tf = tokens[term]
            score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / avg_len))
        if score > 0:
            boost = {"metric": 1.3, "term": 1.25, "entity": 1.1, "attribute": 1.0}[kind]
            scored.append({"kind": kind, "id": ident, "score": round(score * boost, 3)})
    scored.sort(key=lambda s: (-s["score"], s["kind"], s["id"]))
    return scored[:limit]


def build_context(
    project: Project,
    *,
    entities: list[str] | None = None,
    metrics: list[str] | None = None,
    terms: list[str] | None = None,
    question: str | None = None,
    base: str | None = None,
    dialect: str | None = None,
) -> dict[str, Any]:
    entities = list(entities or [])
    metrics = list(metrics or [])
    terms = list(terms or [])
    notes: list[str] = []
    suggestions: list[dict[str, Any]] = []
    if question and not (entities or metrics or terms):
        suggestions = search(project, question)
        top = suggestions[0]["score"] if suggestions else 0.0
        strong = [h for h in suggestions if h["score"] >= 0.6 * top]
        picked_entities = 0
        for hit in strong:
            if hit["kind"] == "metric" and hit["id"] not in metrics:
                metrics.append(hit["id"])
            elif hit["kind"] == "term" and hit["id"] not in terms:
                terms.append(hit["id"])
            elif hit["kind"] in {"entity", "attribute"}:
                ent = hit["id"].split(".")[0]
                if ent not in entities and picked_entities < 3:
                    entities.append(ent)
                    picked_entities += 1
        notes.append("elements were suggested by lexical search; confirm they match the question's meaning")

    glossary_out = []
    for term_id in terms:
        term = project.terms.get(term_id)
        if term is None:
            notes.append(f"unknown glossary term {term_id}")
            continue
        glossary_out.append({k: term[k] for k in ("id", "definition", "synonyms", "maps_to", "disambiguation", "status") if k in term})
        for ref in term.get("maps_to") or []:
            kind, _, target = ref.partition(":")
            if kind == "metric" and target not in metrics:
                metrics.append(target)
            elif kind in {"entity", "attribute", "segment"}:
                ent = target.split(".")[0]
                if ent not in entities:
                    entities.append(ent)

    metric_out = []
    for metric_id in list(metrics):
        metric = project.metrics.get(metric_id)
        if metric is None:
            notes.append(f"unknown metric {metric_id}")
            continue
        metric_out.append(_metric_view(project, metric))
        for dep in _metric_entities(project, metric):
            if dep not in entities:
                entities.append(dep)
    entities = [e for e in entities if e in project.entities]
    if not entities:
        return {"entities": [], "metrics": metric_out, "glossary": glossary_out, "notes": notes + ["no entities selected"],
                "suggestions": suggestions}

    graph = JoinGraph(project)
    plan = graph.plan(entities, base=base or _default_base(project, entities, metrics))
    filters = default_filter_sql(project, plan.entities, dialect)
    # An as-of join already selects the version valid at the fact's time; the "current" filter would drop history.
    as_of_targets = {s.edge.dst for s in plan.steps if s.edge.rel.as_of and s.edge.forward}
    filters = [f for f in filters if not (f["reason"] == "scd2_current" and f["entity"] in as_of_targets)]
    warnings = list(plan.warnings)
    for metric in metric_out:
        warnings.extend(metric.get("warnings", []))
    for entity_id in plan.entities:
        entity = project.entities[entity_id]
        history = entity.raw.get("history") or {}
        if history.get("type") == "scd2" and entity_id not in as_of_targets:
            warnings.append(f"{entity_id} is SCD2 history: use its current filter, or an as-of join on {history.get('valid_from')}/{history.get('valid_to')}")
        if history.get("type") == "snapshot":
            warnings.append(f"{entity_id} is a periodic snapshot: never sum its semi-additive measures across {history.get('snapshot_at')}")

    return {
        "dialect": dialect or project.dialect,
        "entities": [_entity_view(project, e) for e in plan.entities],
        "metrics": metric_out,
        "glossary": glossary_out,
        "join_plan": plan.to_dict(),
        "from_sql": render_from_clause(project, plan, dialect or project.dialect) if not plan.unreachable else None,
        "default_filters": filters,
        "warnings": warnings,
        "verified_queries": _related_queries(project, plan.entities, metrics),
        "notes": notes,
        **({"suggestions": suggestions} if suggestions else {}),
    }


def _default_base(project: Project, entities: list[str], metrics: list[str]) -> str:
    for metric_id in metrics:
        metric = project.metrics.get(metric_id) or {}
        if metric.get("type") == "simple" and metric.get("entity") in entities:
            return metric["entity"]
    facts = [e for e in entities if project.entities[e].raw.get("entity_type") in {"fact", "event", "snapshot"}]
    return facts[0] if facts else entities[0]


def _metric_entities(project: Project, metric: dict[str, Any], depth: int = 0) -> list[str]:
    if depth > 5:
        return []
    if metric.get("type") == "simple":
        return [metric["entity"]] if metric.get("entity") else []
    out: list[str] = []
    refs = list(metric.get("inputs") or []) + [m for m in (metric.get("numerator"), metric.get("denominator")) if m]
    for ref in refs:
        for ent in _metric_entities(project, project.metrics.get(ref) or {}, depth + 1):
            if ent not in out:
                out.append(ent)
    return out


def metric_sql(project: Project, metric: dict[str, Any], alias: str | None = None, depth: int = 0) -> str | None:
    """Render a metric as an aggregate SQL expression (filters as CASE WHEN)."""
    if depth > 5:
        return None
    mtype = metric.get("type")
    if mtype == "simple":
        alias = alias or (entity_alias(metric["entity"]) if metric.get("entity") else None)
        expr = str(metric.get("expr") or "*")
        agg = metric.get("agg", "sum")
        qualified = expr if expr == "*" else _qualify_expr(expr, alias)
        flt = metric.get("filter")
        if flt:
            inner = "1" if expr == "*" else qualified
            qualified = f"CASE WHEN {_qualify_expr(flt, alias)} THEN {inner} END"
        if agg == "count_distinct":
            return f"COUNT(DISTINCT {qualified})"
        if agg == "count" and expr == "*" and not flt:
            return "COUNT(*)"
        function = {"avg": "AVG", "sum": "SUM", "min": "MIN", "max": "MAX", "count": "COUNT", "median": "MEDIAN"}[agg]
        return f"{function}({qualified})"
    if mtype == "ratio":
        num = metric_sql(project, project.metrics.get(metric.get("numerator")) or {}, None, depth + 1)
        den = metric_sql(project, project.metrics.get(metric.get("denominator")) or {}, None, depth + 1)
        return f"CAST({num} AS DOUBLE) / NULLIF({den}, 0)" if num and den else None
    if mtype == "derived":
        expr = str(metric.get("expr") or "")
        for ref in sorted(metric.get("inputs") or [], key=len, reverse=True):
            rendered = metric_sql(project, project.metrics.get(ref) or {}, None, depth + 1)
            if rendered:
                expr = re.sub(rf"\b{re.escape(ref)}\b", f"({rendered})", expr)
        return expr
    return None


def _qualify_expr(sql: str, alias: str | None) -> str:
    from .graph import qualify_condition

    return qualify_condition(sql, alias, None) if alias else sql


def _metric_view(project: Project, metric: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "label", "description", "type", "entity", "agg", "expr", "filter", "numerator", "denominator", "inputs",
            "time", "grains", "unit", "synonyms", "disambiguation", "status", "origin")
    view = {k: metric[k] for k in keys if k in metric}
    view["sql"] = metric_sql(project, metric)
    warnings = []
    if metric.get("status") not in {"approved"}:
        warnings.append(f"metric {metric['id']} is {metric.get('status', 'proposed')} (not approved): say so in the answer")
    if metric.get("type") == "simple":
        entity = project.entities.get(metric.get("entity"))
        if entity:
            from .checksql import _expr_columns

            for column in _expr_columns(str(metric.get("expr") or ""), ""):
                attr = entity.attribute_for_column(column) or {}
                currency = (attr.get("money") or {}).get("currency_attr")
                if currency and currency not in str(metric.get("filter") or ""):
                    warnings.append(f"metric {metric['id']} is in the row currency: GROUP BY {entity_alias(entity.id)}.{currency} or convert before summing")
                if attr.get("scale") and str(attr.get("scale")) not in str(metric.get("expr")):
                    warnings.append(f"{entity.id}.{column} is in minor units (scale {attr['scale']})")
    if warnings:
        view["warnings"] = warnings
    return view


def _entity_view(project: Project, entity_id: str) -> dict[str, Any]:
    raw = project.entities[entity_id].raw
    view: dict[str, Any] = {
        "id": entity_id,
        "alias": entity_alias(entity_id),
        "dataset": raw["source"]["dataset"],
        "entity_type": raw.get("entity_type"),
        "grain": raw.get("grain"),
        "status": raw.get("status"),
    }
    for key in ("label", "description", "history", "caveats"):
        if raw.get(key):
            view[key] = raw[key]
    attrs = []
    for attr in raw.get("attributes") or []:
        item = {"id": attr["id"], "role": attr.get("role")}
        for key in ("column", "expr", "semantic_type", "description", "values", "aggregation", "additivity", "non_additive_over",
                    "money", "scale", "pii", "synonyms", "time"):
            if key in attr:
                item[key] = attr[key]
        attrs.append(item)
    view["attributes"] = attrs
    if raw.get("segments"):
        view["segments"] = [{"id": s["id"], "sql": s["sql"], **({"description": s["description"]} if s.get("description") else {})}
                            for s in raw["segments"]]
    return view


def _related_queries(project: Project, entities: list[str], metrics: list[str]) -> list[dict[str, Any]]:
    wanted = {f"entity:{e}" for e in entities} | {f"metric:{m}" for m in metrics}
    out = []
    for query in project.queries.values():
        uses = set(query.get("uses") or [])
        if uses & wanted:
            out.append({k: query[k] for k in ("id", "question", "sql", "dialect") if k in query})
    return out[:5]


def render_markdown(ctx: dict[str, Any]) -> str:
    """Render a context document as compact Markdown for a prompt."""
    lines: list[str] = []
    if ctx.get("warnings"):
        lines.append("## Warnings")
        lines += [f"- {w}" for w in ctx["warnings"]]
    for entity in ctx.get("entities", []):
        lines.append(f"## Entity `{entity['id']}` ({entity.get('entity_type')}) - `{entity['dataset']}`")
        if entity.get("description"):
            lines.append(entity["description"])
        lines.append(f"- grain: {', '.join(entity.get('grain') or ['unknown'])}")
        for attr in entity["attributes"]:
            extra = []
            for key in ("semantic_type", "aggregation", "additivity", "pii"):
                if attr.get(key):
                    extra.append(f"{key}={attr[key]}")
            if attr.get("values"):
                extra.append("values=" + "|".join(map(str, attr["values"][:12])))
            if attr.get("money"):
                extra.append("currency=" + (attr["money"].get("currency") or attr["money"].get("currency_attr", "")))
            if attr.get("scale"):
                extra.append(f"scale={attr['scale']}")
            column = f" (column {attr['column']})" if attr.get("column") else ""
            lines.append(f"  - `{attr['id']}`{column}: {attr.get('role')}" + (f" [{', '.join(extra)}]" if extra else ""))
        for segment in entity.get("segments", []):
            lines.append(f"  - segment `{segment['id']}`: `{segment['sql']}`")
    if ctx.get("metrics"):
        lines.append("## Metrics")
        for metric in ctx["metrics"]:
            lines.append(f"- `{metric['id']}` ({metric.get('status')}): `{metric.get('sql')}`" + (f" - {metric['description']}" if metric.get("description") else ""))
            if metric.get("disambiguation"):
                lines.append(f"  - note: {metric['disambiguation']}")
    if ctx.get("glossary"):
        lines.append("## Glossary")
        for term in ctx["glossary"]:
            lines.append(f"- **{term['id']}**: {term['definition']}" + (f" ({term['disambiguation']})" if term.get("disambiguation") else ""))
    if ctx.get("from_sql"):
        lines.append("## Join path")
        lines.append("```sql\n" + ctx["from_sql"] + "\n```")
    if ctx.get("default_filters"):
        lines.append("## Default filters (apply unless the question says otherwise)")
        lines += [f"- `{f['sql']}` ({f['reason']})" for f in ctx["default_filters"]]
    if ctx.get("verified_queries"):
        lines.append("## Verified queries")
        for query in ctx["verified_queries"]:
            lines.append(f"- {query['question']}\n```sql\n{query['sql']}\n```")
    if ctx.get("notes"):
        lines.append("## Notes")
        lines += [f"- {n}" for n in ctx["notes"]]
    return "\n".join(lines) + "\n"
