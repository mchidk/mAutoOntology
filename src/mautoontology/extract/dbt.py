"""Extract a snapshot from a dbt project.

Preferred input is the compiled artifacts (``target/manifest.json`` and, when
available, ``target/catalog.json`` from ``dbt docs generate``). Without
artifacts, the extractor falls back to reading ``schema.yml``-style files.

What is captured, all as evidence for later steps:

* models, seeds, snapshots and sources with descriptions and columns;
* column types (catalog) and row counts (catalog stats);
* ``unique`` / ``not_null`` / ``relationships`` / ``accepted_values`` tests;
* lineage (``depends_on``);
* existing dbt Semantic Layer definitions (``semantic_models`` and ``metrics``),
  which are adopted as the source of truth instead of being re-invented.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..snapshot import Column, Dataset, ForeignKey, Snapshot, norm_dataset, norm_ident
from ..yamlio import load_file, plain

_REF_RE = re.compile(r"ref\(\s*['\"]([^'\"]+)['\"](?:\s*,\s*['\"]([^'\"]+)['\"])?\s*\)")
_SOURCE_RE = re.compile(r"source\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)")
_DATASET_TYPES = {"model", "seed", "snapshot"}


def _relation(node: dict[str, Any]) -> str:
    relation = node.get("relation_name")
    if relation:
        return norm_dataset(str(relation).replace('"', "").replace("`", ""))
    parts = [node.get("database"), node.get("schema"), node.get("alias") or node.get("identifier") or node.get("name")]
    return norm_dataset(".".join(str(p) for p in parts if p))


def extract_dbt(project_dir: str | Path, dialect: str | None = None) -> tuple[Snapshot, list[str]]:
    """Extract from a dbt project directory (or a ``target/`` directory)."""
    root = Path(project_dir)
    target = root / "target" if (root / "target").is_dir() else root
    manifest_path = target / "manifest.json"
    warnings: list[str] = []
    if manifest_path.is_file():
        snapshot = _from_manifest(manifest_path, target / "catalog.json", warnings)
    else:
        warnings.append("no target/manifest.json found; falling back to schema.yml files (run `dbt parse` for full fidelity)")
        snapshot = _from_schema_yml(root, warnings)
    if dialect:
        snapshot.dialect = dialect
    return snapshot, warnings


# --------------------------------------------------------------------- manifest
def _from_manifest(manifest_path: Path, catalog_path: Path, warnings: list[str]) -> Snapshot:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    adapter = (manifest.get("metadata") or {}).get("adapter_type") or "ansi"
    snapshot = Snapshot(dialect=_dialect_for_adapter(adapter), sources=[f"dbt:{manifest_path.as_posix()}"])
    by_uid: dict[str, Dataset] = {}

    nodes = dict(manifest.get("nodes") or {})
    for uid, node in nodes.items():
        if node.get("resource_type") not in _DATASET_TYPES:
            continue
        dataset = _dataset_from_node(node, kind=node["resource_type"])
        dataset.dbt_unique_id = uid
        dataset.depends_on = [d for d in (node.get("depends_on") or {}).get("nodes") or [] if not d.startswith("test.")]
        by_uid[uid] = dataset
    for uid, node in (manifest.get("sources") or {}).items():
        dataset = _dataset_from_node(node, kind="source")
        dataset.dbt_unique_id = uid
        by_uid[uid] = dataset

    if catalog_path.is_file():
        snapshot.sources.append(f"dbt-catalog:{catalog_path.as_posix()}")
        _apply_catalog(by_uid, json.loads(catalog_path.read_text(encoding="utf-8")))

    names = {node.get("name"): uid for uid, node in nodes.items() if node.get("resource_type") in _DATASET_TYPES}
    source_names = {
        (src.get("source_name"), src.get("name")): uid for uid, src in (manifest.get("sources") or {}).items()
    }
    for uid, node in nodes.items():
        if node.get("resource_type") == "test":
            _apply_test(uid, node, by_uid, names, source_names, warnings)

    # Lineage between datasets uses relation names (readable), not dbt ids.
    for dataset in by_uid.values():
        dataset.depends_on = [by_uid[d].name for d in dataset.depends_on if d in by_uid]
        snapshot.add(dataset)

    semantic_models = [_semantic_model(sm, by_uid) for sm in (manifest.get("semantic_models") or {}).values()]
    metrics = [_metric(m) for m in (manifest.get("metrics") or {}).values()]
    if semantic_models or metrics:
        snapshot.semantic["dbt"] = {"semantic_models": semantic_models, "metrics": metrics}
    return snapshot


def _dialect_for_adapter(adapter: str) -> str:
    return {
        "snowflake": "snowflake",
        "databricks": "databricks",
        "spark": "spark",
        "bigquery": "bigquery",
        "postgres": "postgres",
        "redshift": "redshift",
        "duckdb": "duckdb",
        "sqlserver": "tsql",
        "fabric": "tsql",
        "synapse": "tsql",
        "trino": "trino",
        "athena": "athena",
        "clickhouse": "clickhouse",
    }.get(str(adapter).lower(), str(adapter).lower() or "ansi")


def _dataset_from_node(node: dict[str, Any], kind: str) -> Dataset:
    dataset = Dataset(name=_relation(node), kind=kind, description=node.get("description") or None)
    for col in (node.get("columns") or {}).values():
        dataset.columns.append(
            Column(name=col.get("name", ""), data_type=col.get("data_type") or "", description=col.get("description") or None)
        )
        constraints = col.get("constraints") or []
        for constraint in constraints:
            ctype = constraint.get("type")
            if ctype == "primary_key":
                dataset.primary_key = [norm_ident(col["name"])]
            elif ctype == "not_null":
                dataset.not_null.append(norm_ident(col["name"]))
            elif ctype == "unique":
                dataset.unique_keys.append([norm_ident(col["name"])])
    for constraint in node.get("constraints") or []:
        if constraint.get("type") == "primary_key" and constraint.get("columns"):
            dataset.primary_key = [norm_ident(c) for c in constraint["columns"]]
    return dataset


def _apply_catalog(by_uid: dict[str, Dataset], catalog: dict[str, Any]) -> None:
    for section in ("nodes", "sources"):
        for uid, entry in (catalog.get(section) or {}).items():
            dataset = by_uid.get(uid)
            if dataset is None:
                continue
            for col in (entry.get("columns") or {}).values():
                name = norm_ident(col.get("name", ""))
                existing = dataset.column(name)
                if existing is None:
                    dataset.columns.append(Column(name=name, data_type=col.get("type") or "", description=col.get("comment")))
                elif not existing.data_type and col.get("type"):
                    existing.data_type = col["type"]
                    existing.family = Column(name="x", data_type=col["type"]).family
            stats = entry.get("stats") or {}
            row_count = (stats.get("row_count") or {}).get("value")
            if isinstance(row_count, (int, float)):
                dataset.row_count = int(row_count)


def _resolve_target(text: str, names: dict[str, str], source_names: dict[tuple[str, str], str]) -> str | None:
    match = _REF_RE.search(text or "")
    if match:
        name = match.group(2) or match.group(1)
        return names.get(name)
    match = _SOURCE_RE.search(text or "")
    if match:
        return source_names.get((match.group(1), match.group(2)))
    return None


def _apply_test(
    uid: str,
    node: dict[str, Any],
    by_uid: dict[str, Dataset],
    names: dict[str, str],
    source_names: dict[tuple[str, str], str],
    warnings: list[str],
) -> None:
    meta = node.get("test_metadata") or {}
    test_name = meta.get("name")
    kwargs = meta.get("kwargs") or {}
    column = node.get("column_name") or kwargs.get("column_name")
    attached = node.get("attached_node") or _resolve_target(str(kwargs.get("model", "")), names, source_names)
    if attached is None:
        deps = [d for d in (node.get("depends_on") or {}).get("nodes") or [] if d in by_uid]
        attached = deps[0] if deps else None
    dataset = by_uid.get(attached or "")
    if dataset is None or not column:
        return
    column = norm_ident(str(column))
    if test_name == "unique":
        if [column] not in dataset.unique_keys:
            dataset.unique_keys.append([column])
    elif test_name == "not_null":
        if column not in dataset.not_null:
            dataset.not_null.append(column)
    elif test_name == "accepted_values":
        values = kwargs.get("values")
        if isinstance(values, list):
            dataset.accepted_values[column] = values
    elif test_name == "relationships":
        parent_uid = _resolve_target(str(kwargs.get("to", "")), names, source_names)
        parent = by_uid.get(parent_uid or "")
        field = kwargs.get("field")
        if parent is None or not field:
            warnings.append(f"{uid}: could not resolve relationships target {kwargs.get('to')!r}")
            return
        dataset.foreign_keys.append(
            ForeignKey(columns=[column], ref_dataset=parent.name, ref_columns=[str(field)], origin="dbt_test", ref=uid)
        )
    elif test_name in {"unique_combination_of_columns", "dbt_utils.unique_combination_of_columns"}:
        cols = kwargs.get("combination_of_columns")
        if isinstance(cols, list) and cols:
            key = [norm_ident(c) for c in cols]
            if key not in dataset.unique_keys:
                dataset.unique_keys.append(key)


def _semantic_model(sm: dict[str, Any], by_uid: dict[str, Dataset]) -> dict[str, Any]:
    deps = (sm.get("depends_on") or {}).get("nodes") or []
    dataset = next((by_uid[d].name for d in deps if d in by_uid), None)
    if dataset is None and sm.get("node_relation"):
        dataset = _relation({"relation_name": (sm["node_relation"] or {}).get("relation_name")})
    return {
        "name": sm.get("name"),
        "description": sm.get("description"),
        "dataset": dataset,
        "defaults": sm.get("defaults") or {},
        "primary_entity": sm.get("primary_entity"),
        "entities": [dict(e) for e in sm.get("entities") or []],
        "dimensions": [dict(d) for d in sm.get("dimensions") or []],
        "measures": [dict(m) for m in sm.get("measures") or []],
    }


def _metric(metric: dict[str, Any]) -> dict[str, Any]:
    params = metric.get("type_params") or {}
    filters = ((metric.get("filter") or {}).get("where_filters") or []) if isinstance(metric.get("filter"), dict) else []
    return {
        "name": metric.get("name"),
        "label": metric.get("label"),
        "description": metric.get("description"),
        "type": metric.get("type"),
        "measure": (params.get("measure") or {}).get("name") if isinstance(params.get("measure"), dict) else params.get("measure"),
        "numerator": _metric_input(params.get("numerator")),
        "denominator": _metric_input(params.get("denominator")),
        "expr": params.get("expr"),
        "inputs": [_metric_input(m) for m in params.get("metrics") or []],
        "filter": " AND ".join(f.get("where_sql_template", "") for f in filters) or None,
    }


def _metric_input(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("name")
    return value


# ---------------------------------------------------------------- schema.yml
def _from_schema_yml(root: Path, warnings: list[str]) -> Snapshot:
    snapshot = Snapshot(sources=[f"dbt-yml:{root.as_posix()}"])
    pending_rels: list[tuple[Dataset, str, str, str]] = []
    semantic_models: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    for path in sorted(list(root.rglob("*.yml")) + list(root.rglob("*.yaml"))):
        if any(part in {"target", "dbt_packages", "logs", "semantics"} for part in path.parts):
            continue
        try:
            data = plain(load_file(path))
        except Exception as error:
            warnings.append(f"{path.as_posix()}: {error}")
            continue
        if not isinstance(data, dict):
            continue
        for model in data.get("models") or []:
            dataset = Dataset(name=model.get("name", ""), kind="model", description=model.get("description"))
            _yml_columns(dataset, model, pending_rels)
            snapshot.add(dataset)
        for source in data.get("sources") or []:
            for table in source.get("tables") or []:
                name = f"{source.get('schema') or source.get('name')}.{table.get('identifier') or table.get('name')}"
                dataset = Dataset(name=name, kind="source", description=table.get("description"))
                _yml_columns(dataset, table, pending_rels)
                snapshot.add(dataset)
        for sm in data.get("semantic_models") or []:
            match = _REF_RE.search(str(sm.get("model", "")))
            semantic_models.append(
                {
                    "name": sm.get("name"),
                    "description": sm.get("description"),
                    "dataset": match.group(2) or match.group(1) if match else None,
                    "defaults": sm.get("defaults") or {},
                    "primary_entity": sm.get("primary_entity"),
                    "entities": sm.get("entities") or [],
                    "dimensions": sm.get("dimensions") or [],
                    "measures": sm.get("measures") or [],
                }
            )
        for metric in data.get("metrics") or []:
            params = metric.get("type_params") or {}
            metrics.append(
                {
                    "name": metric.get("name"),
                    "label": metric.get("label"),
                    "description": metric.get("description"),
                    "type": metric.get("type"),
                    "measure": _metric_input(params.get("measure")),
                    "numerator": _metric_input(params.get("numerator")),
                    "denominator": _metric_input(params.get("denominator")),
                    "expr": params.get("expr"),
                    "inputs": [_metric_input(m) for m in params.get("metrics") or []],
                    "filter": metric.get("filter") if isinstance(metric.get("filter"), str) else None,
                }
            )
    for dataset, column, target, field in pending_rels:
        match = _REF_RE.search(target) or _SOURCE_RE.search(target)
        parent = None
        if match and match.re is _REF_RE:
            parent = snapshot.get(match.group(2) or match.group(1))
        elif match:
            parent = snapshot.get(f"{match.group(1)}.{match.group(2)}") or snapshot.get(match.group(2))
        if parent is None:
            warnings.append(f"{dataset.name}.{column}: unresolved relationships target {target!r}")
            continue
        dataset.foreign_keys.append(ForeignKey(columns=[column], ref_dataset=parent.name, ref_columns=[field], origin="dbt_test"))
    if semantic_models or metrics:
        snapshot.semantic["dbt"] = {"semantic_models": semantic_models, "metrics": metrics}
    return snapshot


def _yml_columns(dataset: Dataset, node: dict[str, Any], pending: list[tuple[Dataset, str, str, str]]) -> None:
    for col in node.get("columns") or []:
        name = norm_ident(str(col.get("name", "")))
        dataset.columns.append(Column(name=name, data_type=col.get("data_type") or "", description=col.get("description")))
        for test in (col.get("data_tests") or []) + (col.get("tests") or []):
            if test == "unique":
                dataset.unique_keys.append([name])
            elif test == "not_null":
                dataset.not_null.append(name)
            elif isinstance(test, dict):
                (tname, args), = list(test.items())[:1] or [(None, None)]
                args = args or {}
                args = args.get("arguments", args) if isinstance(args, dict) else {}
                if tname == "relationships" and args.get("to") and args.get("field"):
                    pending.append((dataset, name, str(args["to"]), str(args["field"])))
                elif tname == "accepted_values" and isinstance(args.get("values"), list):
                    dataset.accepted_values[name] = args["values"]
