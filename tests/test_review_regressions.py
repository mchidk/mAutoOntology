"""Regression tests for issues found in code review (security, correctness, safety)."""

from __future__ import annotations

import json

import pytest
import sqlglot

from mautoontology import profile as prof
from mautoontology.checksql import check_sql
from mautoontology.cli import main
from mautoontology.context import build_context
from mautoontology.export import databricks, snowflake
from mautoontology.model import Project
from mautoontology.propose.pii import classify_name
from mautoontology.propose.run import run_propose
from mautoontology.readonly import run_query
from mautoontology.snapshot import Column, Dataset, Snapshot
from mautoontology.validate import validate_project
from mautoontology.yamlio import dump_text, load_text

DIALECTS = ["snowflake", "databricks", "duckdb", "bigquery", "tsql", "postgres", "redshift", "trino"]


# ------------------------------------------------------------------- security
def test_hostile_identifiers_cannot_inject_statements_into_profiling_sql():
    hostile = 'x") AS c0_nn FROM t; DROP TABLE prod.orders; SELECT COUNT("y'
    snapshot = Snapshot(datasets=[Dataset(name="orders", columns=[Column(hostile, "varchar"), Column("id", "int")])])
    for dialect in DIALECTS:
        for query in prof.emit_stage1(snapshot, dialect, approx=True, sample_pct=5)["queries"]:
            statements = [s for s in sqlglot.parse(query["sql"], read=dialect) if s is not None]
            assert len(statements) == 1, (dialect, query["sql"])
            assert isinstance(statements[0], sqlglot.exp.Select)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_sampling_and_approx_are_valid_per_dialect(dialect, trap_db):
    from mautoontology.extract import extract_duckdb

    plan = prof.emit_stage1(extract_duckdb(trap_db), dialect, approx=True, sample_pct=10)
    query = plan["queries"][0]
    sqlglot.parse_one(query["sql"], read=dialect)
    if dialect == "bigquery":
        assert "TABLESAMPLE SYSTEM" in query["sql"]
    if dialect == "tsql":
        assert "TABLESAMPLE (10.0 PERCENT)" in query["sql"] and "BERNOULLI" not in query["sql"]
    if dialect in {"postgres", "redshift"}:
        assert "APPROX" not in query["sql"].upper() and query["approx"] is False
    if dialect == "redshift":
        assert query["sampled"] is False  # no portable sampling: reported honestly, not silently dropped


def test_duckdb_external_access_is_disabled(trap_db, tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("a\nleaked\n", encoding="utf-8")
    with pytest.raises(Exception, match="(?i)disabled|permission|external"):
        run_query(trap_db, f"SELECT * FROM read_csv('{secret.as_posix()}')", check=False)
    assert run_query(trap_db, "SELECT COUNT(*) FROM orders").rows == [(3000,)]


@pytest.mark.parametrize("name", ["CustomerEmail", "customeremail", "PhoneNumber", "customer_name", "contactname", "name", "user_name"])
def test_pii_names_in_any_case_style(name):
    assert classify_name(name) != "none"


@pytest.mark.parametrize("name", ["product_name", "categoryname", "status", "country_code", "filename"])
def test_non_personal_names(name):
    assert classify_name(name) == "none"


def test_values_that_look_personal_are_never_stored():
    plan = {"stage": 2, "dialect": "duckdb", "queries": [
        {"id": "q1", "kind": "values", "dataset": "t", "column": "contact", "sql": "x"},
        {"id": "q2", "kind": "values", "dataset": "t", "column": "who", "sql": "x"}]}
    rows = {"q1": [{"value": f"person{i}@example.com", "n": 1} for i in range(5)],
            "q2": [{"value": v, "n": 1} for v in ("Anna Hansen", "Bo Jensen", "Carl Nielsen")]}
    profile = prof.ingest(plan, rows)
    for column in ("contact", "who"):
        entry = profile["datasets"]["t"]["columns"][column]
        assert "values" not in entry and entry["pii_by_value"] is True


# ---------------------------------------------------------- check-sql shapes
@pytest.fixture()
def composite_project(tmp_path):
    root = tmp_path / "c"
    sem = root / "semantics"
    sem.mkdir(parents=True)
    (sem / "line.yaml").write_text(dump_text({
        "schema": "mautoontology/v1", "kind": "entity", "id": "line", "source": {"dataset": "lines"},
        "grain": ["order_id", "line_no"], "keys": {"primary": ["order_id", "line_no"]},
        "attributes": [{"id": "order_id", "role": "identifier"}, {"id": "line_no", "role": "identifier"},
                       {"id": "qty", "role": "measure", "aggregation": "sum", "additivity": "additive"}]}), encoding="utf-8")
    (sem / "note.yaml").write_text(dump_text({
        "schema": "mautoontology/v1", "kind": "entity", "id": "note", "source": {"dataset": "notes"}, "grain": ["note_id"],
        "attributes": [{"id": "note_id", "role": "identifier"}, {"id": "order_id", "role": "identifier"},
                       {"id": "line_no", "role": "identifier"}, {"id": "text", "role": "text"}],
        "relationships": [{"id": "note__line", "to": "line", "cardinality": "many_to_one", "status": "approved",
                           "on": [{"from": "order_id", "to": "order_id"}, {"from": "line_no", "to": "line_no"}]}]}),
        encoding="utf-8")
    return Project.load(root)


def test_composite_key_join_in_mixed_orientation(composite_project):
    sql = "SELECT n.text, i.qty FROM notes n JOIN lines i ON n.order_id = i.order_id AND i.line_no = n.line_no"
    result = check_sql(composite_project, sql, "duckdb")
    assert "MAO004" not in result.rules() and result.ok, [f.message for f in result.findings]
    wrong = check_sql(composite_project, "SELECT 1 FROM notes n JOIN lines i ON n.order_id = i.order_id", "duckdb")
    assert "MAO004" in wrong.rules()  # half a composite key is not the relationship


@pytest.fixture()
def as_of_project(example_project):
    path = example_project / "semantics" / "sales" / "order.yaml"
    doc = load_text(path.read_text(encoding="utf-8"))
    doc["relationships"].append({"id": "order__customer_version", "to": "customer_history",
                                 "on": [{"from": "customer_id", "to": "customer_id"}], "cardinality": "many_to_one",
                                 "as_of": {"at": "ordered_at"}, "status": "approved", "reviewed_by": "test"})
    path.write_text(dump_text(doc), encoding="utf-8")
    return example_project


def test_as_of_join_is_valid_and_not_told_to_add_the_current_filter(as_of_project):
    project = Project.load(as_of_project)
    report = validate_project(project)
    assert report.ok, report.to_dict()["issues"]
    good = ("SELECT h.segment, COUNT(*) FROM orders o JOIN customer_history h ON o.customer_id = h.customer_id "
            "AND o.ordered_at >= h.valid_from AND (h.valid_to IS NULL OR o.ordered_at < h.valid_to) "
            "WHERE COALESCE(o.is_deleted, FALSE) = FALSE GROUP BY 1")
    result = check_sql(project, good, "duckdb")
    assert "MAO009" not in result.rules(), [f.message for f in result.findings]
    missing = check_sql(project, "SELECT h.segment FROM orders o JOIN customer_history h ON o.customer_id = h.customer_id "
                                 "WHERE COALESCE(o.is_deleted, FALSE) = FALSE", "duckdb")
    assert any("as-of predicate" in f.message for f in missing.findings)
    ctx = build_context(project, entities=["order", "customer_history"])
    assert not any(f["reason"] == "scd2_current" for f in ctx["default_filters"])
    assert "valid_from" in ctx["from_sql"]
    rows = run_query(as_of_project / "trapshop.duckdb",
                     f"SELECT COUNT(*) {ctx['from_sql']} WHERE COALESCE(order_.is_deleted, FALSE) = FALSE").rows
    assert rows == [(2940,)]  # every order matches exactly one customer version: no fan-out, nothing dropped


def test_staging_table_does_not_resolve_to_the_modelled_entity(example_project):
    snap_path = example_project / "semantics" / "_evidence" / "snapshot.json"
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    staging = json.loads(json.dumps(next(d for d in snap["datasets"] if d["name"] == "orders")))
    staging["name"] = "staging.orders"
    snap["datasets"].append(staging)
    snap_path.write_text(json.dumps(snap), encoding="utf-8")
    result = check_sql(Project.load(example_project), "SELECT COUNT(*) FROM staging.orders", "duckdb")
    assert "MAO002" in result.rules()


# ------------------------------------------------------------- human safety
@pytest.fixture()
def generated(tmp_path, trap_db):
    from mautoontology.extract import extract_duckdb

    root = tmp_path / "g"
    snapshot = extract_duckdb(trap_db)
    snapshot.save(root / "semantics" / "_evidence" / "snapshot.json")
    prof.save_profile(prof.run(trap_db, snapshot), root / "semantics" / "_evidence" / "profile.json")
    run_propose(root)
    return root


def test_comment_only_edit_is_preserved_when_evidence_changes(generated):
    path = generated / "semantics" / "core" / "order.yaml"
    text = path.read_text(encoding="utf-8").replace("  - id: status\n", "  # HUMAN NOTE: statuses come from the OMS\n  - id: status\n")
    path.write_text(text, encoding="utf-8")
    profile_path = generated / "semantics" / "_evidence" / "profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["datasets"]["orders"]["columns"]["status"]["values"].append(["refunded", 1])
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    run_propose(generated)
    assert "# HUMAN NOTE: statuses come from the OMS" in path.read_text(encoding="utf-8")


def test_deleted_entity_file_is_not_recreated(generated):
    path = generated / "semantics" / "core" / "promo.yaml"
    path.unlink()
    summary = run_propose(generated)
    assert not path.exists()
    assert any("promo.yaml was deleted" in w for w in summary.warnings)


# --------------------------------------------------------------- CLI / export
def test_cli_usage_errors_exit_2(tmp_path, capsys):
    assert main(["profile", "run", "--root", str(tmp_path)]) == 2
    assert main(["profile", "ingest", "--root", str(tmp_path), "--plan", "x.json"]) == 2
    assert "needs" in capsys.readouterr().err


def test_databricks_export_refuses_dollar_quote_breakout(example_project, tmp_path):
    path = example_project / "semantics" / "sales" / "order.yaml"
    doc = load_text(path.read_text(encoding="utf-8"))
    doc["description"] = "Orders $$; DROP TABLE x; --"
    path.write_text(dump_text(doc), encoding="utf-8")
    written = databricks.write(Project.load(example_project), tmp_path)
    assert not (tmp_path / "order_metric_view.sql").exists()
    assert (tmp_path / "order_metric_view.yaml").exists()
    assert any(p.name == "databricks_export_notes.md" for p in written)


def test_snowflake_derived_metric_names_do_not_collide(example_project):
    path = example_project / "semantics" / "sales" / "metrics.yaml"
    doc = load_text(path.read_text(encoding="utf-8"))
    doc["metrics"].append({"id": "revenue", "type": "simple", "entity": "order", "agg": "sum", "expr": "amount_cents",
                           "unit": "currency", "status": "approved", "reviewed_by": "t"})
    doc["metrics"].append({"id": "revenue_share", "type": "derived", "expr": "net_revenue / revenue",
                           "inputs": ["net_revenue", "revenue"], "status": "approved", "reviewed_by": "t"})
    path.write_text(dump_text(doc), encoding="utf-8")
    view, _ = snowflake.build(Project.load(example_project))
    share = next(m for m in view["metrics"] if m["name"] == "revenue_share")
    assert share["expr"] == "order.net_revenue / order.revenue"
