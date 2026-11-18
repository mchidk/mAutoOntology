"""The join graph: minimal connecting join paths with fan-out awareness.

Edges come only from relationships in the semantic layer (never guessed).
Traversing a relationship from its many side to its one side is *to-one*
(safe: it cannot multiply rows); the opposite direction is *to-many* (fan-out:
it repeats the rows of everything already joined). Path search prefers approved
over proposed relationships and reports role-playing ambiguity instead of
silently picking a role.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from .model import Project, Relationship
from .sqlutil import canonical_dialect, entity_alias, qualified

_STATUS_COST = {"approved": 1.0, "proposed": 1.25, "draft": 1.6}


@dataclass
class Edge:
    rel: Relationship
    src: str
    dst: str
    to_many: bool  # traversing src -> dst multiplies src rows
    forward: bool = True  # True when src is the relationship's from (child) side

    @property
    def cost(self) -> float:
        return _STATUS_COST.get(self.rel.status, 2.0)


@dataclass
class JoinStep:
    edge: Edge
    join_type: str
    on_sql: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": self.edge.src,
            "to": self.edge.dst,
            "relationship": self.edge.rel.id,
            "direction": "to_many" if self.edge.to_many else "to_one",
            "cardinality": self.edge.rel.cardinality,
            "status": self.edge.rel.status,
            "join": self.join_type,
            "on": self.on_sql,
        }


@dataclass
class JoinPlan:
    base: str
    entities: list[str]
    steps: list[JoinStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)

    @property
    def fanout_entities(self) -> list[str]:
        return [s.edge.dst for s in self.steps if s.edge.to_many]

    def to_dict(self) -> dict[str, Any]:
        return {
            "base": self.base,
            "entities": self.entities,
            "steps": [s.to_dict() for s in self.steps],
            "fanout": self.fanout_entities,
            "warnings": self.warnings,
            "unreachable": self.unreachable,
        }


class JoinGraph:
    def __init__(self, project: Project):
        self.project = project
        self.adj: dict[str, list[Edge]] = {}
        for rel in project.relationships():
            if rel.cardinality == "many_to_many":
                continue  # routed through the bridge entity's own relationships
            if rel.from_entity not in project.entities or rel.to_entity not in project.entities:
                continue
            forward = Edge(rel, rel.from_entity, rel.to_entity, to_many=False)
            backward = Edge(rel, rel.to_entity, rel.from_entity, to_many=rel.cardinality != "one_to_one", forward=False)
            self.adj.setdefault(rel.from_entity, []).append(forward)
            self.adj.setdefault(rel.to_entity, []).append(backward)

    def edges_between(self, a: str, b: str) -> list[Edge]:
        return [e for e in self.adj.get(a, []) if e.dst == b]

    def shortest_path(self, sources: set[str], target: str, avoid_to_many: bool = True) -> list[Edge] | None:
        """Dijkstra from any node in ``sources`` to ``target``; to-many edges cost extra."""
        counter = itertools.count()
        heap: list[tuple[float, int, str, list[Edge]]] = [(0.0, next(counter), s, []) for s in sorted(sources)]
        best: dict[str, float] = {}
        while heap:
            cost, _, node, path = heapq.heappop(heap)
            if node == target:
                return path
            if best.get(node, float("inf")) <= cost:
                continue
            best[node] = cost
            for edge in sorted(self.adj.get(node, []), key=lambda e: (e.rel.id, e.dst)):
                step = edge.cost + (0.5 if (edge.to_many and avoid_to_many) else 0.0)
                heapq.heappush(heap, (cost + step, next(counter), edge.dst, path + [edge]))
        return None

    def plan(self, entities: list[str], base: str | None = None) -> JoinPlan:
        wanted = [e for e in dict.fromkeys(entities) if e]
        if not wanted:
            raise ValueError("no entities given")
        base = base or wanted[0]
        plan = JoinPlan(base=base, entities=[base])
        tree = {base}
        remaining = [e for e in wanted if e != base]
        while remaining:
            best_path: list[Edge] | None = None
            best_target = None
            for target in remaining:
                path = self.shortest_path(tree, target)
                if path is not None and (best_path is None or sum(e.cost for e in path) < sum(e.cost for e in best_path)):
                    best_path, best_target = path, target
            if best_path is None:
                plan.unreachable.extend(remaining)
                plan.warnings.append(
                    "no relationship path connects " + ", ".join(remaining) + " to " + base
                    + "; do not invent a join - ask for the missing relationship"
                )
                break
            for edge in best_path:
                if edge.dst in tree:
                    continue
                alternatives = [e for e in self.edges_between(edge.src, edge.dst) if e.rel.id != edge.rel.id]
                if alternatives:
                    roles = ", ".join(sorted({f"{e.rel.id} (role: {e.rel.role or '-'})" for e in [edge, *alternatives]}))
                    plan.warnings.append(f"{edge.src} -> {edge.dst} is role-playing: choose one of {roles}")
                plan.steps.append(JoinStep(edge, self._join_type(edge), self._on_sql(edge)))
                tree.add(edge.dst)
                plan.entities.append(edge.dst)
            remaining.remove(best_target)
            remaining = [r for r in remaining if r not in tree]
        branches = [s for s in plan.steps if s.edge.to_many]
        if branches:
            plan.warnings.append(
                "fan-out: joining " + ", ".join(f"{s.edge.src}->{s.edge.dst}" for s in branches)
                + " repeats rows; aggregate the many side in a subquery/CTE first, or aggregate only measures of the deepest entity"
            )
        if _diverging(plan.steps, branches):
            plan.warnings.append(
                "chasm trap: two independent to-many branches; measures from different branches must be aggregated "
                "separately (one CTE per branch) and joined at the shared grain"
            )
        return plan

    # ------------------------------------------------------------------ SQL
    def _join_type(self, edge: Edge) -> str:
        # Parent -> child keeps parents without children; optional to-one keeps rows with NULL keys.
        if not edge.forward or edge.rel.optional:
            return "LEFT JOIN"
        return "JOIN"

    def _on_sql(self, edge: Edge) -> str:
        rel = edge.rel
        child, parent = entity_alias(rel.from_entity), entity_alias(rel.to_entity)
        parts = [f"{child}.{c} = {parent}.{p}" for c, p in rel.pairs]
        if rel.as_of:
            at = rel.as_of.get("at")
            parent_entity = self.project.entities.get(rel.to_entity)
            history = (parent_entity.raw.get("history") or {}) if parent_entity else {}
            vf = rel.as_of.get("valid_from") or history.get("valid_from")
            vt = rel.as_of.get("valid_to") or history.get("valid_to")
            if at and vf:
                parts.append(f"{child}.{at} >= {parent}.{vf}")
            if at and vt:
                parts.append(f"({parent}.{vt} IS NULL OR {child}.{at} < {parent}.{vt})")
        if rel.condition:
            parts.append(rel.condition.replace("{from}", child).replace("{to}", parent))
        return " AND ".join(parts)


def _diverging(steps: list[JoinStep], branches: list[JoinStep]) -> bool:
    """True when two to-many steps lie on different branches (not one nested chain)."""
    parent = {s.edge.dst: s.edge.src for s in steps}

    def ancestors(node: str) -> set[str]:
        seen = {node}
        while node in parent:
            node = parent[node]
            seen.add(node)
        return seen

    for a, b in itertools.combinations(branches, 2):
        if a.edge.dst not in ancestors(b.edge.src) and b.edge.dst not in ancestors(a.edge.src):
            return True
    return False


def qualify_condition(sql: str, alias: str, dialect: str | None = None) -> str:
    """Prefix unqualified columns in a predicate with ``alias``."""
    read = canonical_dialect(dialect) or None
    try:
        tree = sqlglot.parse_one(sql, read=read)
    except sqlglot.errors.ParseError:
        return sql
    for column in tree.find_all(exp.Column):
        if not column.table:
            column.set("table", exp.to_identifier(alias))
    return tree.sql(dialect=read)


def render_from_clause(project: Project, plan: JoinPlan, dialect: str | None = None) -> str:
    """Render ``FROM ... JOIN ...`` for a join plan (aliases are entity ids)."""
    entities = project.entities
    lines = [f"FROM {qualified(entities[plan.base].dataset, dialect)} AS {entity_alias(plan.base)}"]
    for step in plan.steps:
        dst = step.edge.dst
        lines.append(f"{step.join_type} {qualified(entities[dst].dataset, dialect)} AS {entity_alias(dst)} ON {step.on_sql}")
    return "\n".join(lines)


def default_filter_sql(project: Project, entity_ids: list[str], dialect: str | None = None) -> list[dict[str, str]]:
    out = []
    for entity_id in entity_ids:
        entity = project.entities.get(entity_id)
        if entity is None:
            continue
        for flt in entity.raw.get("default_filters") or []:
            out.append({"entity": entity_id, "id": flt["id"], "reason": flt["reason"],
                        "sql": qualify_condition(flt["sql"], entity_alias(entity_id), dialect)})
    return out
