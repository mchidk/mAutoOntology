"""check-sql: verify candidate SQL against the semantic layer and explain how to repair it.

Research on LLM question answering over enterprise SQL found that an
ontology-based *query check plus repair* loop is what moves accuracy the most,
and that schema-linking and join errors dominate failures. This module is that
check. It parses SQL in any sqlglot dialect, analyses every SELECT scope (CTEs,
derived tables, set operations; UNNEST/LATERAL helper scopes are skipped),
tracks column lineage through CTEs and derived tables, maps tables to entities,
and reports findings with stable rule ids and concrete repair hints:

========  ========  =============================================================
rule      severity  meaning
========  ========  =============================================================
MAO001    error     not exactly one read-only SELECT (parse error, DML/DDL, many statements)
MAO002    error     table is not part of the semantic layer (or unknown to the catalog)
MAO003    error     column does not exist on the table (or is ambiguous)
MAO004    error     join does not follow a declared relationship (wrong keys or no relationship)
MAO005    error     cartesian product: a table joined without any join predicate
MAO006    error     fan-out: a measure is aggregated after a to-many join (values repeat)
MAO007    error     chasm trap: measures from two independent to-many branches in one SELECT
MAO008    warning   aggregation matches a metric but ignores its filter, or is ambiguous between metrics
MAO009    warning   a default filter (soft delete, current SCD2 version) is missing
MAO010    warning   money summed across currencies (no currency grouping/filter)
MAO011    warning   semi-additive measure (balance/stock) summed across its snapshot time
MAO012    error     identifier aggregated with SUM/AVG
MAO013    info      join uses a relationship that is not approved (proposed/deprecated)
MAO014    warning   personal data in the output (columns, stars, value-collecting aggregates)
MAO015    info      aggregated measure is stored in minor units (apply its scale)
MAO016    warning   non-additive measure (price, rate, ratio) summed
========  ========  =============================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from .model import Entity, Project, Relationship
from .readonly import ReadOnlyViolation, assert_single_select
from .snapshot import norm_ident
from .sqlutil import canonical_dialect

RULES = {
    "MAO001": "not a single read-only SELECT",
    "MAO002": "unknown table",
    "MAO003": "unknown or ambiguous column",
    "MAO004": "join does not follow a declared relationship",
    "MAO005": "cartesian product",
    "MAO006": "fan-out: measure aggregated after a to-many join",
    "MAO007": "chasm trap",
    "MAO008": "metric filter missing or metric ambiguous",
    "MAO009": "default filter missing",
    "MAO010": "money summed across currencies",
    "MAO011": "semi-additive measure summed over time",
    "MAO012": "identifier aggregated",
    "MAO013": "relationship not approved",
    "MAO014": "personal data in output",
    "MAO015": "measure stored in minor units",
    "MAO016": "non-additive measure summed",
}
_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}
_UNSAFE_AGGS = (exp.Sum, exp.Avg)
_VALUE_COLLECTING = {"GroupConcat", "ArrayAgg", "ArrayUniqueAgg", "ArrayConcatAgg", "StringAgg", "ListAgg"}


@dataclass
class Finding:
    rule: str
    severity: str
    message: str
    hint: str | None = None
    entity: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {"rule": self.rule, "severity": self.severity, "title": RULES[self.rule], "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        if self.entity:
            out["entity"] = self.entity
        return out


@dataclass
class CheckResult:
    sql: str
    dialect: str
    findings: list[Finding] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    relationships: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    def rules(self) -> list[str]:
        return sorted({f.rule for f in self.findings})

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dialect": self.dialect,
            "entities": self.entities,
            "relationships": self.relationships,
            "findings": [f.to_dict() for f in sorted(self.findings, key=lambda f: (_SEVERITY_RANK[f.severity], f.rule))],
        }


@dataclass
class _Lineage:
    """Where a column of a CTE/derived table comes from."""

    entity: Entity | None
    column: str | None
    fanned: bool = False       # rows were multiplied by a to-many join inside the derived scope
    aggregated: bool = False   # the derived scope grouped/aggregated (pre-aggregation is safe)
    fanned_by: tuple[str, ...] = ()


@dataclass
class _Source:
    alias: str
    entity: Entity | None
    table: str
    columns: set[str]
    derived: bool = False
    lineage: dict[str, _Lineage] = field(default_factory=dict)
    colmap: dict[str, str] = field(default_factory=dict)  # visible name -> physical column (passthrough derived tables)

    def physical(self, column: str) -> str:
        return self.colmap.get(column, column)


class _ScopeView:
    """Resolved view of one SELECT scope: its own sources, joins, filters and aggregates."""

    def __init__(self, checker: SqlChecker, scope: Scope):
        self.checker = checker
        self.scope = scope
        self.select: exp.Expression = scope.expression
        self.sources: dict[str, _Source] = {}
        for alias, (_node, source) in scope.selected_sources.items():
            key = norm_ident(alias)
            if isinstance(source, exp.Table) and source.name:
                self.sources[key] = checker.resolve_table(key, source)
            elif isinstance(source, Scope):
                self.sources[key] = checker.derived_source(key, source)
            else:
                self.sources[key] = _Source(alias=key, entity=None, table=key, columns=set(), derived=True)

    @property
    def has_unknown_derived(self) -> bool:
        return any(s.derived and not s.columns for s in self.sources.values())

    # -- column resolution ---------------------------------------------------
    def owner(self, column: exp.Column) -> _Source | None:
        name = norm_ident(column.name)
        if column.table:
            return self.sources.get(norm_ident(column.table))
        holders = [s for s in self.sources.values() if name in s.columns]
        return holders[0] if len(holders) == 1 else None

    def own_nodes(self, node: exp.Expression | None, kind: type) -> list[exp.Expression]:
        """Nodes of ``kind`` inside ``node`` that belong to this scope (not nested selects)."""
        if node is None:
            return []
        out = []
        for found in node.find_all(kind):
            ancestor = found.find_ancestor(exp.Select)
            if ancestor is self.select or (ancestor is None and node is not self.select):
                out.append(found)
        return out

    def join_nodes(self) -> list[exp.Expression]:
        return [j.args["on"] for j in self.select.args.get("joins") or [] if j.args.get("on") is not None]

    def predicates(self) -> list[exp.Expression]:
        nodes = self.join_nodes()
        if self.select.args.get("where") is not None:
            nodes.append(self.select.args["where"].this)
        return nodes

    def filter_columns(self) -> set[tuple[str, str]]:
        """(alias, physical column) pairs referenced in WHERE / JOIN ON / HAVING / USING."""
        out: set[tuple[str, str]] = set()
        nodes = self.predicates()
        if self.select.args.get("having") is not None:
            nodes.append(self.select.args["having"])
        for node in nodes:
            for column in self.own_nodes(node, exp.Column):
                src = self.owner(column)
                if src is not None:
                    out.add((src.alias, src.physical(norm_ident(column.name))))
        return out

    def equality_filtered(self) -> set[tuple[str, str]]:
        """Columns pinned to a single value (col = literal, col IN (single)), not inside an OR."""
        out: set[tuple[str, str]] = set()
        nodes = list(self.join_nodes())
        if self.select.args.get("where") is not None:
            nodes.append(self.select.args["where"].this)
        for root in nodes:
            for eq in self.own_nodes(root, exp.EQ):
                if _under_or(eq, root):
                    continue
                for col, other in ((eq.this, eq.expression), (eq.expression, eq.this)):
                    if isinstance(col, exp.Column) and not other.find(exp.Column):
                        src = self.owner(col)
                        if src is not None:
                            out.add((src.alias, src.physical(norm_ident(col.name))))
            for node in self.own_nodes(root, exp.In):
                if _under_or(node, root):
                    continue
                if isinstance(node.this, exp.Column) and len(node.expressions) == 1:
                    src = self.owner(node.this)
                    if src is not None:
                        out.add((src.alias, src.physical(norm_ident(node.this.name))))
        return out

    def group_columns(self) -> set[tuple[str, str]]:
        out: set[tuple[str, str]] = set()
        group = self.select.args.get("group")
        if group is None:
            return out
        projections = {p.alias_or_name: p for p in self.select.expressions}
        targets: list[exp.Expression] = []
        if group.args.get("all"):  # GROUP BY ALL
            targets = [p for p in self.select.expressions if p.find(exp.AggFunc) is None]
        for expr in group.expressions:
            target = expr
            if isinstance(expr, exp.Literal) and expr.is_int:  # GROUP BY 1
                index = int(expr.name) - 1
                if 0 <= index < len(self.select.expressions):
                    target = self.select.expressions[index]
            elif isinstance(expr, exp.Column) and not expr.table and expr.name in projections:
                target = projections[expr.name]
            targets.append(target)
        for target in targets:
            for column in target.find_all(exp.Column):
                src = self.owner(column)
                if src is not None:
                    out.add((src.alias, src.physical(norm_ident(column.name))))
        return out

    @property
    def aggregates(self) -> bool:
        return self.select.args.get("group") is not None or any(
            p.find(exp.AggFunc) is not None and p.find(exp.Window) is None for p in self.select.expressions
        ) or bool(self.select.args.get("distinct"))


class SqlChecker:
    def __init__(self, project: Project, dialect: str | None = None):
        self.project = project
        self.dialect = canonical_dialect(dialect or project.dialect) or ""
        self._lineage: dict[int, dict[str, _Lineage]] = {}
        self._passthrough: dict[int, tuple[Entity, dict[str, str]]] = {}

    # ------------------------------------------------------------- resolution
    def resolve_table(self, alias: str, table: exp.Table) -> _Source:
        parts = [p for p in (table.catalog, table.db, table.name) if p]
        full = ".".join(norm_ident(p) for p in parts)
        entity = self.project.entity_for_dataset(full)
        columns = self.project.entity_columns(entity) if entity else set()
        if entity is None and self.project.snapshot is not None:
            ds = self.project.snapshot.get(full)
            if ds is not None:
                columns = {c.name for c in ds.columns}
        return _Source(alias=alias, entity=entity, table=full, columns=columns)

    def derived_source(self, alias: str, scope: Scope) -> _Source:
        key = id(scope.expression)
        lineage = self._lineage.get(key, {})
        if key in self._passthrough:
            entity, colmap = self._passthrough[key]
            return _Source(alias=alias, entity=entity, table=alias, columns=set(colmap), derived=True, colmap=colmap)
        return _Source(alias=alias, entity=None, table=alias, columns=set(lineage), derived=True, lineage=lineage)

    # ----------------------------------------------------------------- check
    def check(self, sql: str) -> CheckResult:
        result = CheckResult(sql=sql, dialect=self.dialect or "ansi")
        try:
            tree = assert_single_select(sql, self.dialect or None)
        except ReadOnlyViolation as error:
            result.findings.append(Finding("MAO001", "error", str(error), "Submit exactly one SELECT (or WITH ... SELECT) statement."))
            return result
        try:
            scopes = traverse_scope(tree)
        except Exception as error:  # sqlglot scope errors on exotic syntax
            result.findings.append(Finding("MAO001", "error", f"could not analyse query scopes: {error}"))
            return result
        entities: set[str] = set()
        rels_used: set[str] = set()
        views: dict[int, _ScopeView] = {}
        for scope in scopes:  # children (CTEs, derived tables) come before their parents
            if not isinstance(scope.expression, exp.Select):
                continue
            view = _ScopeView(self, scope)
            views[id(scope.expression)] = view
            for source in view.sources.values():
                if source.entity is not None:
                    entities.add(source.entity.id)
                elif not source.derived:
                    self._unknown_table(result, source)
            self._check_columns(result, view)
            edges = self._check_joins(result, view, rels_used)
            self._check_aggregates(result, view, edges)
            self._check_default_filters(result, view, edges)
            self._record_lineage(view, edges)
        self._check_pii(result, tree, views)
        result.entities = sorted(entities)
        result.relationships = sorted(rels_used)
        result.findings = _dedupe(result.findings)
        return result

    # --------------------------------------------------------------- lineage
    def _record_lineage(self, view: _ScopeView, edges: list[tuple[_Source, _Source, Relationship]]) -> None:
        """Describe this scope's output columns for the scopes that select from it."""
        key = id(view.select)
        table_sources = [s for s in view.sources.values()]
        simple = (len(table_sources) == 1 and table_sources[0].entity is not None and not view.select.args.get("joins")
                  and not view.aggregates and view.select.args.get("limit") is None)
        lineage: dict[str, _Lineage] = {}
        colmap: dict[str, str] = {}
        aggregated = view.aggregates
        fanned_cache: dict[str, list[str]] = {}
        for projection in view.select.expressions:
            if isinstance(projection, exp.Star) or (isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)):
                targets = [view.sources[norm_ident(projection.table)]] if isinstance(projection, exp.Column) and projection.table \
                    and norm_ident(projection.table) in view.sources else list(view.sources.values())
                for src in targets:
                    for column in src.columns:
                        lineage[column] = self._column_lineage(src, column, edges, aggregated, fanned_cache)
                        colmap[column] = src.physical(column)
                continue
            name = norm_ident(projection.alias_or_name)
            inner = projection.this if isinstance(projection, exp.Alias) else projection
            if isinstance(inner, exp.Column):
                src = view.owner(inner)
                if src is not None:
                    lineage[name] = self._column_lineage(src, norm_ident(inner.name), edges, aggregated, fanned_cache)
                    colmap[name] = src.physical(norm_ident(inner.name))
                    continue
            lineage[name] = _Lineage(entity=None, column=None, aggregated=True)
        self._lineage[key] = lineage
        if simple and len(colmap) == len(lineage):
            self._passthrough[key] = (table_sources[0].entity, colmap)

    def _column_lineage(self, src: _Source, column: str, edges, aggregated: bool, cache: dict[str, list[str]]) -> _Lineage:
        if src.derived and src.entity is None:
            inherited = src.lineage.get(column) or _Lineage(entity=None, column=None)
            return _Lineage(inherited.entity, inherited.column, inherited.fanned and not aggregated,
                            inherited.aggregated or aggregated, inherited.fanned_by)
        if src.alias not in cache:
            cache[src.alias] = self._fanout(src, edges)
        by = cache[src.alias]
        return _Lineage(src.entity, src.physical(column), fanned=bool(by) and not aggregated, aggregated=aggregated, fanned_by=tuple(by))

    # ------------------------------------------------------------------ rules
    def _unknown_table(self, result: CheckResult, source: _Source) -> None:
        snap = self.project.snapshot
        known = snap is not None and snap.get(source.table) is not None
        if known:
            result.findings.append(Finding("MAO002", "warning", f"table {source.table} exists but is not modelled as an entity",
                                           "Model it first (mautoontology propose) or use a modelled entity."))
        else:
            options = ", ".join(sorted(e.dataset for e in self.project.entities.values())[:12])
            result.findings.append(Finding("MAO002", "error", f"table {source.table} is not in the semantic layer or catalog",
                                           f"Use one of the modelled datasets: {options}"))

    def _check_columns(self, result: CheckResult, view: _ScopeView) -> None:
        projection_aliases = {norm_ident(p.alias) for p in view.select.expressions if p.alias}
        for column in view.own_nodes(view.select, exp.Column):
            name = norm_ident(column.name)
            if not name or isinstance(column.this, exp.Star):
                continue
            if column.table:
                src = view.sources.get(norm_ident(column.table))
                if src is None or not src.columns:
                    continue  # outer reference / unknown table (reported elsewhere)
                if name not in src.columns:
                    suggestion = _closest(name, src.columns)
                    entity = src.entity.id if src.entity else src.table
                    result.findings.append(Finding(
                        "MAO003", "error", f"column {column.table}.{name} does not exist on {src.table}",
                        f"Did you mean {column.table}.{suggestion}?" if suggestion else None, entity))
            else:
                holders = [s for s in view.sources.values() if name in s.columns]
                using = {norm_ident(u.name) for j in view.select.args.get("joins") or [] for u in j.args.get("using") or []}
                if len(holders) > 1 and name not in using:
                    result.findings.append(Finding(
                        "MAO003", "error", f"column {name} is ambiguous (in {', '.join(s.alias for s in holders)})",
                        f"Qualify it, e.g. {holders[0].alias}.{name}"))
                elif not holders and name not in projection_aliases and view.sources and not view.has_unknown_derived:
                    if all(s.columns for s in view.sources.values()):
                        result.findings.append(Finding("MAO003", "error", f"column {name} does not exist on any table in this SELECT"))

    def _relationship_between(self, a: Entity, b: Entity) -> list[Relationship]:
        out = []
        for rel in self.project.relationships(include_inactive=True):
            if {rel.from_entity, rel.to_entity} == {a.id, b.id} and rel.cardinality != "many_to_many":
                out.append(rel)
        return out

    def _join_pairs(self, view: _ScopeView) -> tuple[dict[tuple[str, str], list[tuple[str, str]]], set[str]]:
        """Equality pairs between two sources, keyed by the sorted alias pair, oriented to that key."""
        pairs: dict[tuple[str, str], list[tuple[str, str]]] = {}
        connected: set[str] = set()

        def add(a: _Source, a_col: str, b: _Source, b_col: str) -> None:
            if a.alias == b.alias:
                if a_col != b_col:  # self join written on one alias is not a join
                    return
            key = tuple(sorted((a.alias, b.alias)))
            oriented = (a.physical(a_col), b.physical(b_col)) if key == (a.alias, b.alias) else (b.physical(b_col), a.physical(a_col))
            pairs.setdefault(key, []).append(oriented)
            connected.update(key)

        for node in view.predicates():
            for eq in view.own_nodes(node, exp.EQ):
                left_cols = list(eq.this.find_all(exp.Column))
                right_cols = list(eq.expression.find_all(exp.Column))
                if len(left_cols) != 1 or len(right_cols) != 1:
                    continue
                a, b = view.owner(left_cols[0]), view.owner(right_cols[0])
                if a is None or b is None or a.alias == b.alias:
                    continue
                add(a, norm_ident(left_cols[0].name), b, norm_ident(right_cols[0].name))
            # any predicate touching two sources connects them (e.g. a range or as-of condition)
            for predicate in view.own_nodes(node, exp.Predicate):
                aliases = {view.owner(c).alias for c in predicate.find_all(exp.Column) if view.owner(c) is not None}
                if len(aliases) == 2:
                    connected.update(aliases)
        aliases = list(view.sources)
        for join in view.select.args.get("joins") or []:
            right = norm_ident(join.this.alias_or_name)
            if right not in view.sources:
                continue
            earlier = aliases[: aliases.index(right)]
            names = [norm_ident(u.name) for u in join.args.get("using") or []]
            if str(join.args.get("method") or "").upper() == "NATURAL":
                names = sorted({c for alias in earlier for c in view.sources[alias].columns} & view.sources[right].columns)
            for name in names:
                left = next((view.sources[a] for a in earlier if name in view.sources[a].columns), None)
                if left is not None:
                    add(left, name, view.sources[right], name)
        return pairs, connected

    def _check_joins(self, result: CheckResult, view: _ScopeView, rels_used: set[str]) -> list[tuple[_Source, _Source, Relationship]]:
        """Validate join predicates; return the (child, parent, relationship) edges actually used."""
        pairs, connected = self._join_pairs(view)
        edges: list[tuple[_Source, _Source, Relationship]] = []
        for (a_alias, b_alias), cols in pairs.items():
            a, b = view.sources[a_alias], view.sources[b_alias]
            if a.entity is None or b.entity is None:
                continue
            rels = self._relationship_between(a.entity, b.entity)
            match, child, parent = None, a, b
            for rel in rels:
                forward = rel.from_entity == a.entity.id and set(rel.pairs) <= set(cols)
                reverse = {(y, x) for x, y in cols}
                backward = rel.from_entity == b.entity.id and set(rel.pairs) <= reverse
                if a.entity.id == b.entity.id:
                    if set(rel.pairs) <= set(cols):
                        match, child, parent = rel, a, b
                        break
                    if set(rel.pairs) <= reverse:
                        match, child, parent = rel, b, a
                        break
                    continue
                if forward or backward:
                    match = rel
                    child, parent = (a, b) if forward else (b, a)
                    break
            if match is None:
                if rels:
                    expected = " / ".join(
                        " AND ".join(f"{_alias_for(r.from_entity, a, b)}.{c} = {_alias_for(r.to_entity, a, b)}.{p}" for c, p in r.pairs)
                        + f"  ({r.id}{', role ' + r.role if r.role else ''})" for r in rels)
                    result.findings.append(Finding(
                        "MAO004", "error",
                        f"join {a_alias} <-> {b_alias} uses {', '.join(f'{x}={y}' for x, y in cols)}, which matches no declared relationship",
                        f"Use: {expected}", a.entity.id))
                else:
                    path = self._suggest_path(a.entity.id, b.entity.id)
                    result.findings.append(Finding(
                        "MAO004", "error", f"no relationship is declared between {a.entity.id} and {b.entity.id}",
                        f"Join through: {path}" if path else "Do not invent this join; ask for the relationship to be modelled.",
                        a.entity.id))
                continue
            rels_used.add(match.id)
            if match.status in {"deprecated", "rejected"}:
                result.findings.append(Finding("MAO013", "warning", f"join uses {match.status} relationship {match.id}", None, match.from_entity))
            elif match.status != "approved":
                result.findings.append(Finding("MAO013", "info", f"join uses relationship {match.id} with status '{match.status}'", None, match.from_entity))
            edges.append((child, parent, match))
            self._check_as_of(result, view, child, parent, match)
        table_aliases = [alias for alias, src in view.sources.items() if not src.derived or src.entity is not None]
        if len(table_aliases) > 1:
            unconnected = [a for a in table_aliases if a not in connected]
            if len(unconnected) == len(table_aliases):
                unconnected = unconnected[1:]  # nothing joined at all: the first table is the base
            for alias in unconnected:
                src = view.sources[alias]
                result.findings.append(Finding(
                    "MAO005", "error", f"{alias} ({src.table}) is joined without any join predicate (cartesian product)",
                    "Add an ON condition that follows a declared relationship.", src.entity.id if src.entity else None))
        return edges

    def _check_as_of(self, result: CheckResult, view: _ScopeView, child: _Source, parent: _Source, rel: Relationship) -> None:
        history = (parent.entity.raw.get("history") or {}) if parent.entity else {}
        if history.get("type") != "scd2" or rel.as_of is None:
            return
        used = view.filter_columns()
        valid_from = norm_ident(rel.as_of.get("valid_from") or history.get("valid_from", ""))
        if (parent.alias, valid_from) not in used:
            at = rel.as_of.get("at")
            valid_to = rel.as_of.get("valid_to") or history.get("valid_to")
            result.findings.append(Finding(
                "MAO009", "warning", f"{parent.alias} is SCD2 history joined without the as-of predicate of {rel.id}",
                f"Add: {child.alias}.{at} >= {parent.alias}.{valid_from} AND "
                f"({parent.alias}.{valid_to} IS NULL OR {child.alias}.{at} < {parent.alias}.{valid_to})",
                parent.entity.id if parent.entity else None))

    def _suggest_path(self, a: str, b: str) -> str | None:
        from .graph import JoinGraph

        path = JoinGraph(self.project).shortest_path({a}, b)
        if not path:
            return None
        return " -> ".join([a] + [f"{e.dst} (via {e.rel.id})" for e in path])

    # ------------------------------------------------------------- aggregates
    def _check_aggregates(self, result: CheckResult, view: _ScopeView, edges: list[tuple[_Source, _Source, Relationship]]) -> None:
        aggs = view.own_nodes(view.select, exp.AggFunc)
        if not aggs:
            return
        group_cols = view.group_columns()
        pinned = view.equality_filtered()
        filtered = view.filter_columns()
        fanned_entities: dict[str, list[str]] = {}
        for agg in aggs:
            distinct = isinstance(agg.this, exp.Distinct)
            columns = list(agg.find_all(exp.Column))
            if not columns:
                continue
            owners: dict[str, _Source] = {}
            for column in columns:
                src = view.owner(column)
                if src is not None:
                    owners[src.alias] = src
            counts = isinstance(agg, (exp.Sum, exp.Avg, exp.Count)) and not isinstance(agg.this, exp.Star) and not distinct
            for src in owners.values():
                if src.entity is None:
                    self._derived_fanout(result, src, agg, columns, view, counts)
                    continue
                for column in columns:
                    if view.owner(column) is not src:
                        continue
                    attr = src.entity.attribute_for_column(src.physical(norm_ident(column.name)))
                    if attr is None:
                        continue
                    label = f"{src.alias}.{norm_ident(column.name)}"
                    if isinstance(agg, _UNSAFE_AGGS) and attr.get("role") == "identifier":
                        result.findings.append(Finding(
                            "MAO012", "error", f"{agg.key.upper()}({label}) aggregates an identifier",
                            f"Use COUNT(DISTINCT {label}) to count, or aggregate a measure.", src.entity.id))
                    if isinstance(agg, exp.Sum) and attr.get("role") == "measure":
                        self._money_rules(result, src, attr, label, group_cols, pinned)
                        if attr.get("additivity") == "semi_additive":
                            over = norm_ident(str(attr.get("non_additive_over") or ""))
                            over_col = src.entity.column_of(over) or over
                            if over and (src.alias, over_col) not in group_cols and (src.alias, over_col) not in pinned:
                                result.findings.append(Finding(
                                    "MAO011", "warning", f"SUM({label}) adds a semi-additive measure across {over}",
                                    f"Filter to one {over} (e.g. the latest) or GROUP BY {src.alias}.{over_col}; use AVG/last value across time.",
                                    src.entity.id))
                        elif attr.get("additivity") == "non_additive":
                            result.findings.append(Finding(
                                "MAO016", "warning", f"SUM({label}) adds a non-additive measure ({attr.get('semantic_type', 'rate/price')})",
                                "Use AVG, or recompute from additive components (e.g. SUM(quantity * unit_price)).", src.entity.id))
                if counts:
                    multiplied = self._fanout(src, edges)
                    if multiplied:
                        fanned_entities.setdefault(src.entity.id, []).extend(multiplied)
            if agg.find_ancestor(exp.Window) is None:
                self._metric_rules(result, view, agg, owners, filtered)
        if len(fanned_entities) >= 2 and self._independent_branches(fanned_entities):
            names = ", ".join(sorted(fanned_entities))
            result.findings.append(Finding(
                "MAO007", "error", f"chasm trap: measures of {names} are aggregated in one SELECT across independent to-many joins",
                "Aggregate each branch in its own CTE at the shared grain, then join the CTEs."))
        else:
            for entity_id, by in fanned_entities.items():
                via = ", ".join(sorted(set(by)))
                result.findings.append(Finding(
                    "MAO006", "error",
                    f"measures of {entity_id} are aggregated after joining {via} (one {entity_id} row per many {via} rows); totals are inflated",
                    f"Aggregate {via} in a subquery/CTE grouped by the {entity_id} key first, or use COUNT(DISTINCT ...) for counts.",
                    entity_id))

    def _derived_fanout(self, result: CheckResult, src: _Source, agg: exp.Expression, columns: list[exp.Column],
                        view: _ScopeView, counts: bool) -> None:
        """Aggregating a CTE/derived column whose rows were multiplied inside that CTE."""
        if not counts:
            return
        for column in columns:
            if view.owner(column) is not src:
                continue
            lineage = src.lineage.get(norm_ident(column.name))
            if lineage is None or not lineage.fanned or lineage.entity is None:
                continue
            attr = lineage.entity.attribute_for_column(lineage.column or "") or {}
            if attr.get("role") == "identifier" and isinstance(agg, exp.Count):
                pass  # counting keys of a fanned table still overcounts; report it below
            via = ", ".join(lineage.fanned_by)
            result.findings.append(Finding(
                "MAO006", "error",
                f"{agg.sql(dialect=self.dialect or None)} aggregates {src.alias}.{norm_ident(column.name)}, which {src.alias} repeats "
                f"once per {via} row (to-many join inside the CTE/derived table); totals are inflated",
                f"Aggregate {via} first (GROUP BY the {lineage.entity.id} key) or select {lineage.entity.id} without that join.",
                lineage.entity.id))

    def _fanout(self, src: _Source, edges: list[tuple[_Source, _Source, Relationship]]) -> list[str]:
        """Entities whose join multiplies rows of ``src`` (walks the joined tree from src)."""
        multiplied: list[str] = []
        seen = {src.alias}
        frontier = [(src.alias, False)]
        while frontier:
            alias, fanned = frontier.pop()
            for child, parent, rel in edges:
                for here, there, to_many in ((parent.alias, child, rel.cardinality != "one_to_one"), (child.alias, parent, False)):
                    if here != alias or there.alias in seen:
                        continue
                    seen.add(there.alias)
                    now = fanned or to_many
                    if now:
                        multiplied.append(there.entity.id if there.entity else there.alias)
                    frontier.append((there.alias, now))
        return multiplied

    @staticmethod
    def _independent_branches(fanned: dict[str, list[str]]) -> bool:
        """Chasm when entity A is fanned out by B's branch and B by A's (neither contains the other)."""
        items = list(fanned.items())
        for i, (a, by_a) in enumerate(items):
            for b, by_b in items[i + 1:]:
                if b in by_a and a in by_b:
                    return True
        return False

    def _money_rules(self, result: CheckResult, src: _Source, attr: dict[str, Any], label: str,
                     group_cols: set[tuple[str, str]], pinned: set[tuple[str, str]]) -> None:
        currency_attr = (attr.get("money") or {}).get("currency_attr")
        if currency_attr and src.entity is not None:
            currency_col = src.entity.column_of(currency_attr) or norm_ident(currency_attr)
            if (src.alias, currency_col) not in group_cols and (src.alias, currency_col) not in pinned:
                result.findings.append(Finding(
                    "MAO010", "warning", f"SUM({label}) adds amounts in different currencies ({src.alias}.{currency_col})",
                    f"GROUP BY {src.alias}.{currency_col}, filter to one currency, or convert with an FX rate first.",
                    src.entity.id))
        if attr.get("scale"):
            result.findings.append(Finding(
                "MAO015", "info", f"{label} is stored in minor units (scale {attr['scale']})",
                f"Multiply by {attr['scale']} to report major units.", src.entity.id if src.entity else None))

    def _metric_rules(self, result: CheckResult, view: _ScopeView, agg: exp.Expression,
                      owners: dict[str, _Source], filtered: set[tuple[str, str]]) -> None:
        if len(owners) != 1:
            return
        src = next(iter(owners.values()))
        if src.entity is None:
            return
        agg_name = _agg_name(agg)
        agg_cols = {src.physical(norm_ident(c.name)) for c in agg.find_all(exp.Column)}
        matches = []
        for metric in self.project.metrics.values():
            if metric.get("type") != "simple" or metric.get("entity") != src.entity.id or metric.get("status") in {"rejected", "deprecated"}:
                continue
            if metric.get("agg") != agg_name:
                continue
            if agg_cols and _expr_columns(str(metric.get("expr", "")), self.dialect) == agg_cols:
                matches.append(metric)
        if not matches:
            return
        applied = []
        for metric in matches:
            needed = _expr_columns(str(metric.get("filter") or ""), self.dialect)
            if not needed or all((src.alias, c) in filtered or _in_case(agg, c) for c in needed):
                applied.append(metric)
        if applied:
            return
        if len(matches) == 1:
            metric = matches[0]
            result.findings.append(Finding(
                "MAO008", "warning",
                f"{agg.sql(dialect=self.dialect or None)} looks like metric '{metric['id']}' but its filter is not applied",
                f"Add: {metric['filter']}  (metric '{metric['id']}')", src.entity.id))
        else:
            options = "; ".join(f"{m['id']}: {m.get('filter') or 'no filter'}" for m in matches)
            result.findings.append(Finding(
                "MAO008", "warning", f"{agg.sql(dialect=self.dialect or None)} is ambiguous between metrics ({options})",
                "Pick the metric the question means and apply its filter explicitly.", src.entity.id))

    def _check_default_filters(self, result: CheckResult, view: _ScopeView, edges: list[tuple[_Source, _Source, Relationship]]) -> None:
        used = view.filter_columns()
        as_of_parents = {parent.alias for _child, parent, rel in edges if rel.as_of}
        for src in view.sources.values():
            if src.entity is None or src.derived:
                continue
            for flt in src.entity.raw.get("default_filters") or []:
                if flt["reason"] == "scd2_current" and src.alias in as_of_parents:
                    continue  # an as-of join selects the right version; the current filter would drop history
                needed = {src.physical(c) for c in _expr_columns(flt["sql"], self.dialect)}
                if needed and not any((src.alias, c) in used for c in needed):
                    rendered = _qualify(flt["sql"], src.alias, self.dialect)
                    reason = "current version of SCD2 history" if flt["reason"] == "scd2_current" else flt["reason"].replace("_", " ")
                    result.findings.append(Finding(
                        "MAO009", "warning", f"{src.table} read without its default filter '{flt['id']}' ({reason})",
                        f"Add: {rendered}  (omit only if the question explicitly includes these rows)", src.entity.id))

    # -------------------------------------------------------------------- PII
    def _check_pii(self, result: CheckResult, tree: exp.Expression, views: dict[int, _ScopeView]) -> None:
        for select in _output_selects(tree):
            view = views.get(id(select))
            if view is None:
                continue
            for projection in select.expressions:
                agg = projection.find(exp.AggFunc)
                if agg is not None and type(agg).__name__ not in _VALUE_COLLECTING and projection.find(exp.Window) is None:
                    continue  # COUNT/SUM/... do not return the values themselves
                if isinstance(projection, exp.Star) or (isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)):
                    table = norm_ident(projection.table) if isinstance(projection, exp.Column) and projection.table else None
                    sources = [view.sources[table]] if table and table in view.sources else list(view.sources.values())
                    for src in sources:
                        for column in sorted(src.columns):
                            self._report_pii(result, src, column, star=True)
                    continue
                for column in projection.find_all(exp.Column):
                    src = view.owner(column)
                    if src is not None:
                        self._report_pii(result, src, norm_ident(column.name))

    def _report_pii(self, result: CheckResult, src: _Source, column: str, star: bool = False) -> None:
        entity, physical = src.entity, src.physical(column)
        if entity is None:
            lineage = src.lineage.get(column)
            if lineage is None or lineage.entity is None:
                return
            entity, physical = lineage.entity, lineage.column or column
        attr = entity.attribute_for_column(physical) or {}
        pii = attr.get("pii")
        if pii and pii != "none":
            severity = "warning" if pii in {"direct_identifier", "sensitive", "contact"} else "info"
            how = " (via SELECT *)" if star else ""
            result.findings.append(Finding(
                "MAO014", severity, f"output includes personal data {src.alias}.{column} ({pii}){how}",
                "Return it only if the user is entitled to it; select explicit non-personal columns, aggregate or mask.",
                entity.id))


# ---------------------------------------------------------------- utilities
def check_sql(project: Project, sql: str, dialect: str | None = None) -> CheckResult:
    return SqlChecker(project, dialect).check(sql)


def _output_selects(tree: exp.Expression) -> list[exp.Expression]:
    """SELECT nodes whose rows reach the caller (every branch of a set operation)."""
    if isinstance(tree, exp.Select):
        return [tree]
    if isinstance(tree, exp.SetOperation):
        return _output_selects(tree.this) + _output_selects(tree.expression)
    if isinstance(tree, exp.Subquery):
        return _output_selects(tree.this)
    return []


def _under_or(node: exp.Expression, root: exp.Expression) -> bool:
    parent = node.parent
    while parent is not None and parent is not root.parent:
        if isinstance(parent, exp.Or):
            return True
        if parent is root:
            break
        parent = parent.parent
    return False


def _agg_name(agg: exp.Expression) -> str:
    if isinstance(agg, exp.Count):
        return "count_distinct" if isinstance(agg.this, exp.Distinct) else "count"
    return {"sum": "sum", "avg": "avg", "min": "min", "max": "max", "median": "median"}.get(agg.key, agg.key)


def _expr_columns(sql: str, dialect: str) -> set[str]:
    if not sql.strip() or sql.strip() == "*":
        return set()
    try:
        tree = sqlglot.parse_one(sql, read=dialect or None)
    except sqlglot.errors.ParseError:
        return set()
    return {norm_ident(c.name) for c in tree.find_all(exp.Column)}


def _in_case(agg: exp.Expression, column: str) -> bool:
    """A metric filter may be applied inside the aggregate: SUM(CASE WHEN status <> 'x' THEN amount END)."""
    for case in agg.find_all(exp.Case, exp.If):
        if any(norm_ident(c.name) == column for c in case.find_all(exp.Column)):
            return True
    return agg.args.get("filter") is not None


def _qualify(sql: str, alias: str, dialect: str) -> str:
    from .graph import qualify_condition

    return qualify_condition(sql, alias, dialect)


def _alias_for(entity_id: str, a: _Source, b: _Source) -> str:
    if a.entity is not None and a.entity.id == entity_id:
        return a.alias
    return b.alias


def _closest(name: str, options: set[str]) -> str | None:
    from difflib import get_close_matches

    match = get_close_matches(name, sorted(options), n=1, cutoff=0.6)
    return match[0] if match else None


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, str]] = set()
    out = []
    for finding in findings:
        key = (finding.rule, finding.message)
        if key not in seen:
            seen.add(key)
            out.append(finding)
    return out
