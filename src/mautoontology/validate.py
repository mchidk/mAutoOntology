"""Validate a semantic layer: schema, references, semantics, governance and drift.

Findings carry stable ids (``V###``) and a severity. ``error`` means the layer
is wrong or unusable (a broken reference, a relationship whose parent is not a
key, an identifier with SUM); ``warning`` means it is risky or incomplete
(approved without evidence, money without currency, ambiguous synonyms);
``info`` is advisory (drift such as new columns).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot
from jsonschema import Draft202012Validator

from .checksql import check_sql
from .model import Project, load_schema
from .snapshot import Snapshot, norm_ident
from .sqlutil import canonical_dialect

_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}


@dataclass
class Issue:
    code: str
    severity: str
    where: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity, "where": self.where, "message": self.message}


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)
    files: int = 0

    def add(self, code: str, severity: str, where: str, message: str) -> None:
        self.issues.append(Issue(code, severity, where, message))

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    def counts(self) -> dict[str, int]:
        out = {"error": 0, "warning": 0, "info": 0}
        for issue in self.issues:
            out[issue.severity] += 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "files": self.files,
            "counts": self.counts(),
            "issues": [i.to_dict() for i in sorted(self.issues, key=lambda i: (_SEVERITY_RANK[i.severity], i.code, i.where))],
        }


def validate_project(project: Project, *, drift_snapshot: Snapshot | None = None, check_queries: bool = True) -> ValidationReport:
    report = ValidationReport(files=len(project.files))
    for message in project.load_errors:
        report.add("V001", "error", message.split(":")[0], message)
    for message in project.duplicates:
        report.add("V002", "error", "semantics", f"duplicate id: {message}")
    _schema(project, report)
    if not project.config:
        report.add("V003", "warning", "semantics/mautoontology.yaml", "no config file (kind: config); run `mautoontology init`")
    dialect = canonical_dialect(project.dialect) or None
    for entity in project.entities.values():
        _entity(project, entity, report, dialect)
    _metrics(project, report, dialect)
    _glossary(project, report)
    _synonyms(project, report)
    if check_queries:
        _verified_queries(project, report)
    snap = drift_snapshot or project.snapshot
    if drift_snapshot is not None or snap is not None:
        _drift(project, snap, report, explicit=drift_snapshot is not None)
    return report


# ------------------------------------------------------------------- schema
def _schema(project: Project, report: ValidationReport) -> None:
    validator = Draft202012Validator(load_schema())
    for path, data in project.files.items():
        for error in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
            location = "/".join(str(p) for p in error.absolute_path) or "(root)"
            report.add("V010", "error", f"{project.rel(path)}#{location}", error.message[:300])


# ------------------------------------------------------------------- entity
def _entity(project: Project, entity, report: ValidationReport, dialect: str | None) -> None:
    where = project.rel(entity.path)
    raw = entity.raw
    physical = project.physical_columns(entity)
    columns = physical if physical is not None else project.entity_columns(entity)
    snap = project.snapshot
    if snap is not None and snap.get(entity.dataset) is None:
        report.add("V020", "error", where, f"source dataset {entity.dataset} is not in the catalog snapshot")
    attr_ids = {a.get("id") for a in entity.attributes}
    for key_name, cols in (("grain", raw.get("grain") or []), ("keys.primary", (raw.get("keys") or {}).get("primary") or [])):
        for col in cols:
            if norm_ident(col) not in columns and col not in attr_ids:
                report.add("V021", "error", where, f"{key_name} column {col} does not exist")
    if not raw.get("grain") and raw.get("entity_type") not in {"event"}:
        report.add("V022", "warning", where, "no grain declared: what does one row represent?")

    time_attrs = {a["id"] for a in entity.attributes if a.get("role") == "time"}
    for attr in entity.attributes:
        aw = f"{where}#attributes/{attr.get('id')}"
        column = attr.get("column") or attr.get("id")
        if attr.get("expr"):
            _parse_or_report(attr["expr"], dialect, report, aw, "V023")
        elif physical is not None and norm_ident(str(column)) not in physical:
            report.add("V024", "error", aw, f"column {column} does not exist on {entity.dataset}")
        if attr.get("role") == "identifier" and attr.get("aggregation") in {"sum", "avg"}:
            report.add("V025", "error", aw, "identifiers must not have a sum/avg aggregation")
        if attr.get("additivity") == "semi_additive":
            over = attr.get("non_additive_over")
            if not over:
                report.add("V026", "error", aw, "semi_additive measure needs non_additive_over (the snapshot time attribute)")
            elif over not in time_attrs and entity.attribute(over) is None:
                report.add("V026", "error", aw, f"non_additive_over {over} is not an attribute of {entity.id}")
        if attr.get("semantic_type") in {"money", "price"} and not attr.get("money"):
            report.add("V027", "warning", aw, "money without currency (set money.currency or money.currency_attr)")
        currency_attr = (attr.get("money") or {}).get("currency_attr")
        if currency_attr and entity.attribute(currency_attr) is None:
            report.add("V028", "error", aw, f"currency_attr {currency_attr} is not an attribute of {entity.id}")
        if attr.get("pii") not in (None, "none") and attr.get("values"):
            report.add("V029", "error", aw, "PII attributes must not list sample values")
        if attr.get("role") == "measure" and not attr.get("aggregation"):
            report.add("V030", "warning", aw, "measure without a default aggregation")
        _governance(attr, report, aw)

    for flt in raw.get("default_filters") or []:
        _parse_or_report(flt["sql"], dialect, report, f"{where}#default_filters/{flt['id']}", "V031")
    for seg in raw.get("segments") or []:
        _parse_or_report(seg["sql"], dialect, report, f"{where}#segments/{seg['id']}", "V031")
    history = raw.get("history") or {}
    for key in ("valid_from", "valid_to", "snapshot_at"):
        if history.get(key) and norm_ident(history[key]) not in columns and entity.attribute(history[key]) is None:
            report.add("V032", "error", where, f"history.{key} column {history[key]} does not exist")

    for rel in raw.get("relationships") or []:
        _relationship(project, entity, rel, report, f"{where}#relationships/{rel.get('id')}")
    _governance(raw, report, where)


def _relationship(project: Project, entity, rel: dict[str, Any], report: ValidationReport, where: str) -> None:
    target = project.entities.get(rel.get("to"))
    if target is None:
        report.add("V040", "error", where, f"relationship target entity {rel.get('to')} does not exist")
        return
    if rel.get("cardinality") == "many_to_many":
        bridge = project.entities.get(rel.get("through"))
        if bridge is None:
            report.add("V041", "error", where, f"through entity {rel.get('through')} does not exist")
        else:
            targets = {r.get("to") for r in bridge.raw.get("relationships") or []}
            if not {entity.id, target.id} <= targets:
                report.add("V042", "error", where, f"bridge {bridge.id} must have relationships to both {entity.id} and {target.id}")
        _governance(rel, report, where)
        return
    child_cols = project.physical_columns(entity) or project.entity_columns(entity)
    parent_cols = project.physical_columns(target) or project.entity_columns(target)
    pairs = rel.get("on") or []
    for pair in pairs:
        if child_cols and norm_ident(pair["from"]) not in child_cols:
            report.add("V043", "error", where, f"join column {entity.id}.{pair['from']} does not exist")
        if parent_cols and norm_ident(pair["to"]) not in parent_cols:
            report.add("V043", "error", where, f"join column {target.id}.{pair['to']} does not exist")
    parent_key = [norm_ident(p["to"]) for p in pairs]
    if parent_key and target.candidate_keys() and not target.is_key(parent_key) and not rel.get("as_of"):
        report.add("V044", "error", where,
                   f"parent columns ({', '.join(parent_key)}) are not a key of {target.id}: the join would fan out; "
                   "declare the key on the parent or fix the relationship")
    if rel.get("cardinality") == "one_to_one":
        child_key = [norm_ident(p["from"]) for p in pairs]
        if entity.candidate_keys() and not entity.is_key(child_key):
            report.add("V045", "warning", where, f"one_to_one but ({', '.join(child_key)}) is not a key of {entity.id}")
    as_of = rel.get("as_of") or {}
    if as_of and (target.raw.get("history") or {}).get("type") != "scd2" and not (as_of.get("valid_from") and as_of.get("valid_to")):
        report.add("V046", "warning", where, f"as_of join to {target.id}, which has no scd2 history declared")
    _governance(rel, report, where)


def _governance(element: dict[str, Any], report: ValidationReport, where: str) -> None:
    status = element.get("status")
    if status == "approved":
        if not element.get("reviewed_by") and element.get("origin") not in {"dbt", "snowflake", "databricks"}:
            report.add("V050", "warning", where, "approved without reviewed_by")
        if not element.get("evidence") and element.get("origin") == "generated":
            report.add("V051", "warning", where, "approved generated element without evidence")


def _parse_or_report(sql: str, dialect: str | None, report: ValidationReport, where: str, code: str) -> None:
    try:
        sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError as error:
        report.add(code, "error", where, f"SQL does not parse: {str(error).splitlines()[0][:200]}")


# ------------------------------------------------------------------ metrics
def _metrics(project: Project, report: ValidationReport, dialect: str | None) -> None:
    for metric_id, metric in project.metrics.items():
        where = f"{project.rel(project.metric_files[metric_id])}#metrics/{metric_id}"
        mtype = metric.get("type")
        if mtype == "simple":
            entity = project.entities.get(metric.get("entity"))
            if entity is None:
                report.add("V060", "error", where, f"entity {metric.get('entity')} does not exist")
                continue
            columns = project.entity_columns(entity) | {a["id"] for a in entity.attributes}
            for key in ("expr", "filter"):
                text = metric.get(key)
                if not text or text.strip() == "*":
                    continue
                try:
                    tree = sqlglot.parse_one(text, read=dialect)
                except sqlglot.errors.ParseError as error:
                    report.add("V061", "error", where, f"{key} does not parse: {str(error).splitlines()[0][:200]}")
                    continue
                for column in tree.find_all(sqlglot.exp.Column):
                    if norm_ident(column.name) not in columns:
                        report.add("V062", "error", where, f"{key} references unknown column {column.name} on {entity.id}")
            for column in _columns(metric.get("expr"), dialect):
                attr = entity.attribute_for_column(column)
                if attr and attr.get("role") == "identifier" and metric.get("agg") in {"sum", "avg"}:
                    report.add("V063", "error", where, f"{metric.get('agg')} over identifier {column}")
                if attr and attr.get("additivity") == "semi_additive" and metric.get("agg") == "sum":
                    report.add("V064", "warning", where, f"sum over semi-additive {column}: aggregate per {attr.get('non_additive_over')} first")
                if attr and (attr.get("money") or {}).get("currency_attr") and metric.get("agg") == "sum":
                    currency = attr["money"]["currency_attr"]
                    # unit == the currency attribute declares "amounts are per row currency; always group by it".
                    if currency not in str(metric.get("filter") or "") and metric.get("unit") != currency:
                        report.add("V065", "warning", where, f"sums {column} across currencies; filter or convert by {attr['money']['currency_attr']}")
            if metric.get("time") and entity.attribute(metric["time"]) is None:
                report.add("V066", "error", where, f"time attribute {metric['time']} does not exist on {entity.id}")
            elif metric.get("time") and entity.attribute(metric["time"]).get("role") != "time":
                report.add("V066", "warning", where, f"time attribute {metric['time']} does not have role time")
        elif mtype == "ratio":
            for key in ("numerator", "denominator"):
                if metric.get(key) not in project.metrics:
                    report.add("V067", "error", where, f"{key} metric {metric.get(key)} does not exist")
        elif mtype == "derived":
            for ref in metric.get("inputs") or []:
                if ref not in project.metrics:
                    report.add("V067", "error", where, f"input metric {ref} does not exist")
            _parse_or_report(str(metric.get("expr", "")), dialect, report, where, "V061")
        if _cycle(project, metric_id):
            report.add("V068", "error", where, "metric definition is cyclic")
        _governance(metric, report, where)


def _columns(sql: str | None, dialect: str | None) -> set[str]:
    if not sql or sql.strip() == "*":
        return set()
    try:
        return {norm_ident(c.name) for c in sqlglot.parse_one(sql, read=dialect).find_all(sqlglot.exp.Column)}
    except sqlglot.errors.ParseError:
        return set()


def _cycle(project: Project, start: str) -> bool:
    stack, seen = [(start, ())], set()
    while stack:
        node, path = stack.pop()
        metric = project.metrics.get(node) or {}
        deps = list(metric.get("inputs") or []) + [m for m in (metric.get("numerator"), metric.get("denominator")) if m]
        for dep in deps:
            if dep == start:
                return True
            if dep not in seen:
                seen.add(dep)
                stack.append((dep, path + (node,)))
    return False


# ----------------------------------------------------------------- glossary
def _glossary(project: Project, report: ValidationReport) -> None:
    for term_id, term in project.terms.items():
        where = f"glossary#{term_id}"
        for ref in term.get("maps_to") or []:
            if not project.resolve_ref(ref):
                report.add("V070", "error", where, f"maps_to {ref} does not exist")
        if len(term.get("maps_to") or []) > 1 and not term.get("disambiguation"):
            report.add("V071", "warning", where, "term maps to several elements but has no disambiguation")


def _synonyms(project: Project, report: ValidationReport) -> None:
    """The same word must not silently mean two things."""
    owners: dict[str, list[str]] = {}
    disambiguated: set[str] = set()
    def note(word: str, owner: str, element: dict[str, Any]) -> None:
        owners.setdefault(word.lower().strip(), []).append(owner)
        if element.get("disambiguation"):
            disambiguated.add(word.lower().strip())
    for metric_id, metric in project.metrics.items():
        for word in [metric_id.replace("_", " ")] + list(metric.get("synonyms") or []):
            note(word, f"metric:{metric_id}", metric)
    for term_id, term in project.terms.items():
        for word in list(term.get("synonyms") or []):
            note(word, f"term:{term_id}", term)
    for entity in project.entities.values():
        for attr in entity.attributes:
            for word in attr.get("synonyms") or []:
                note(word, f"attribute:{entity.id}.{attr['id']}", attr)
    term_targets = {f"term:{tid}": set(term.get("maps_to") or []) for tid, term in project.terms.items()}
    for word, who in owners.items():
        distinct = sorted(set(who))
        # A glossary term that maps to an element sharing the word is consistent, not a clash.
        covered = {target for owner in distinct for target in term_targets.get(owner, set())}
        distinct = [owner for owner in distinct if owner not in covered]
        if len(distinct) > 1:
            severity = "warning" if word in disambiguated else "error"
            report.add("V072", severity, "synonyms", f"'{word}' is used by {', '.join(distinct)}"
                       + ("" if severity == "warning" else "; add a disambiguation note or remove the duplicate synonym"))


# ---------------------------------------------------------- verified queries
def _verified_queries(project: Project, report: ValidationReport) -> None:
    for query_id, query in project.queries.items():
        where = f"verified_queries#{query_id}"
        for ref in query.get("uses") or []:
            if not project.resolve_ref(ref):
                report.add("V080", "error", where, f"uses {ref}, which does not exist")
        result = check_sql(project, query["sql"], query.get("dialect"))
        for finding in result.findings:
            if finding.severity == "error":
                report.add("V081", "error", where, f"{finding.rule}: {finding.message}")
            elif finding.severity == "warning":
                report.add("V082", "warning", where, f"{finding.rule}: {finding.message}")


# -------------------------------------------------------------------- drift
def _drift(project: Project, snapshot: Snapshot | None, report: ValidationReport, explicit: bool) -> None:
    if snapshot is None:
        return
    for entity in project.entities.values():
        ds = snapshot.get(entity.dataset)
        where = project.rel(entity.path)
        if ds is None:
            if explicit:
                report.add("V090", "error", where, f"drift: dataset {entity.dataset} no longer exists")
            continue
        physical = {c.name: c for c in ds.columns}
        modelled = set()
        for attr in entity.attributes:
            if attr.get("expr"):
                continue
            column = norm_ident(str(attr.get("column") or attr.get("id")))
            modelled.add(column)
            if column not in physical:
                if explicit:
                    report.add("V091", "error", f"{where}#attributes/{attr.get('id')}", f"drift: column {column} was removed")
            elif attr.get("family") and physical[column].family not in {attr["family"], "other"}:
                report.add("V092", "warning", f"{where}#attributes/{attr.get('id')}",
                           f"drift: type changed from {attr['family']} to {physical[column].family}")
        new_columns = sorted(set(physical) - modelled)
        if new_columns and explicit:
            report.add("V093", "info", where, f"drift: new columns not modelled yet: {', '.join(new_columns)}")
    modelled_datasets = {e.dataset for e in project.entities.values()}
    if explicit:
        for ds in snapshot.datasets:
            if not any(project.entity_for_dataset(ds.name) for _ in [0]) and ds.name not in modelled_datasets:
                report.add("V094", "info", "semantics", f"drift: dataset {ds.name} is not modelled yet")
