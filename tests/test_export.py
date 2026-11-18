"""Exporters: Ossie validates against the official schema; dbt validates with MetricFlow when available."""

from __future__ import annotations

import importlib.util

import pytest

from mautoontology.export import TARGETS, databricks, dbt, export, ossie, snowflake
from mautoontology.model import Project
from mautoontology.yamlio import load_file

from .conftest import EXAMPLE


@pytest.fixture(scope="module")
def project():
    return Project.load(EXAMPLE)


def test_ossie_document_is_valid_against_vendored_schema(project):
    doc = ossie.build(project)
    assert ossie.validate_document(doc) == []
    assert doc["version"] == "0.3.0.dev0"
    order = next(d for d in doc["datasets"] if d["name"] == "order")
    assert order["primary_key"] == ["order_id"]
    assert {m["name"] for m in order["metrics"]} >= {"net_revenue", "gross_revenue", "order_count"}
    ext = order["nonportable_extensions"][0]
    assert ext["vendor_name"] == "mautoontology" and ext["data"]["default_filters"][0]["id"] == "not_deleted"
    email = next(f for d in doc["datasets"] if d["name"] == "customer" for f in d["fields"] if f["name"] == "email")
    assert "values" not in email["nonportable_extensions"][0]["data"]  # PII values never exported
    assert {r["name"] for r in doc["relationships"]} >= {"order__billing_address", "order__shipping_address"}


def test_ossie_rejects_invalid_documents():
    assert ossie.validate_document({"version": "0.3.0.dev0", "name": "x", "datasets": []})


def test_dbt_export_shape(project):
    doc, notes = dbt.build(project)
    models = {m["name"]: m for m in doc["semantic_models"]}
    assert models["order"]["defaults"] == {"agg_time_dimension": "ordered_at"}
    assert {"name": "order", "type": "primary", "expr": "order_id"} in models["order"]["entities"]
    assert models["account_balance"]["primary_entity"] == "account_balance"
    balance = next(m for m in models["account_balance"]["measures"] if m["name"] == "balance")
    assert balance["non_additive_dimension"]["name"] == "snapshot_date"
    metrics = {m["name"]: m for m in doc["metrics"]}
    assert metrics["net_revenue"]["filter"] == "NOT {{ Dimension('order__status') }} IN ('cancelled', 'returned')"
    assert metrics["average_order_value"]["type_params"]["numerator"] == {"name": "net_revenue"}
    assert "units_sold" not in metrics and any("units_sold" in n for n in notes)  # no time dimension -> skipped, explained


@pytest.mark.skipif(importlib.util.find_spec("metricflow_semantic_interfaces") is None,
                    reason="MetricFlow validator not installed (pip install dbt-core)")
def test_dbt_export_passes_metricflow_validation(project):
    from metricflow_semantic_interfaces.parsing.dir_to_model import (
        parse_yaml_files_to_validation_ready_semantic_manifest,
    )
    from metricflow_semantic_interfaces.parsing.objects import YamlConfigFile
    from metricflow_semantic_interfaces.validations.semantic_manifest_validator import (
        SemanticManifestValidator,
    )
    from ruamel.yaml import YAML

    doc, _ = dbt.build(project)
    docs = [{"project_configuration": {"time_spine_table_configurations": [
        {"location": "main.metricflow_time_spine", "column_name": "date_day", "grain": "day"}]}}]
    for model in doc["semantic_models"]:
        model = dict(model)
        model["node_relation"] = {"alias": model.pop("model").split("'")[1], "schema_name": "main"}
        docs.append({"semantic_model": model})
    docs += [{"metric": m} for m in doc.get("metrics", [])]
    import io

    buffer = io.StringIO()
    YAML(typ="safe", pure=True).dump_all(docs, buffer)
    result = parse_yaml_files_to_validation_ready_semantic_manifest([YamlConfigFile(filepath="x.yml", contents=buffer.getvalue())])
    issues = SemanticManifestValidator().validate_semantic_manifest(result.semantic_manifest)
    assert not issues.has_blocking_issues, [i.as_readable_str() for i in issues.all_issues]


def test_snowflake_semantic_view_shape(project):
    doc, _ = snowflake.build(project)
    tables = {t["name"]: t for t in doc["tables"]}
    order = tables["order"]
    assert order["primary_key"] == {"columns": ["order_id"]}
    assert any(f["name"] == "amount_cents" and f["expr"] == "amount_cents * 0.01" for f in order["facts"])
    status = next(d for d in order["dimensions"] if d["name"] == "status")
    assert status["is_enum"] is True and "shipped" in status["sample_values"]
    assert all("sample_values" not in d for d in tables["customer"]["dimensions"] if d["name"] == "email")
    assert {v["name"] for v in doc["verified_queries"]} == {q for q in project.queries}
    assert doc["metrics"][0]["name"] == "average_order_value"
    assert "not_deleted" in doc["module_custom_instructions"]["sql_generation"]


def test_databricks_metric_view(project, tmp_path):
    view = databricks.build_view(project, "order")
    assert view["version"] == "1.1" and view["source"] == "orders"
    names = []

    def walk(joins):
        for join in joins:
            names.append(join["name"])
            walk(join.get("joins", []))

    walk(view["joins"])
    assert len(names) == len(set(names))  # join names are unique across nesting
    assert any(m["name"] == "average_order_value" and "MEASURE(" in m["expr"] for m in view["measures"])
    paths = databricks.write(project, tmp_path)
    text = (tmp_path / "order_metric_view.yaml").read_text(encoding="utf-8")
    assert "'on':" in text and "\n    on:" not in text  # quoted for YAML 1.1 parsers
    assert any(p.suffix == ".sql" for p in paths)
    assert "WITH METRICS" in (tmp_path / "order_metric_view.sql").read_text(encoding="utf-8")


def test_export_all_targets_write_files(project, tmp_path):
    for target in TARGETS:
        written = export(project, target, tmp_path / target)
        assert written and all(p.exists() for p in written)
    md = (tmp_path / "markdown" / "SEMANTIC_LAYER.md").read_text(encoding="utf-8")
    assert "erDiagram" in md and "net_revenue" in md
    assert load_file(tmp_path / "ossie" / "ossie.yaml")["name"] == "trapshop"


def test_unknown_target(project, tmp_path):
    with pytest.raises(ValueError):
        export(project, "tableau", tmp_path)
