"""Profiling SQL generation/ingestion and relationship discovery benchmarks (gold from outside the code)."""

from __future__ import annotations

import sqlglot

from mautoontology import profile as prof
from mautoontology.extract import extract_duckdb
from mautoontology.propose.candidates import key_candidates, relationship_candidates
from mautoontology.propose.relationships import score_candidates
from mautoontology.snapshot import Column, Dataset, Snapshot

from .conftest import warehouse

TARGET_DIALECTS = ["snowflake", "databricks", "bigquery", "postgres", "tsql", "duckdb", "redshift", "trino"]


def _discover(db):
    snapshot = extract_duckdb(db)
    profile = prof.run(db, snapshot)
    keys = key_candidates(snapshot, profile)
    candidates = relationship_candidates(snapshot, [k for k in keys if k.proven is not False])
    decision = score_candidates(snapshot, candidates, profile)
    found = {(r.candidate.child, r.candidate.child_columns[0], r.candidate.parent, r.candidate.parent_columns[0]) for r in decision.accepted}
    return found, decision


def test_trap_warehouse_discovery_precision_and_recall(trap_db):
    found, decision = _discover(trap_db)
    gold = warehouse.GOLD_RELATIONSHIPS
    precision = len(found & gold) / len(found)
    recall = len(found & gold) / len(gold)
    assert precision == 1.0, sorted(found - gold)
    assert recall == 1.0, sorted(gold - found)
    assert not (found & warehouse.DECOYS)
    roles = {r.candidate.child_columns[0]: r.candidate.role for r in decision.accepted if r.candidate.child == "orders"}
    assert roles["billing_address_id"] == "billing" and roles["shipping_address_id"] == "shipping"
    one_to_one = {r.candidate.child for r in decision.accepted if r.cardinality == "one_to_one"}
    assert "shipments" in one_to_one


def test_tpch_discovery_precision_and_recall(tpch_db):
    found, _ = _discover(tpch_db)
    gold = warehouse.TPCH_GOLD
    assert len(found & gold) / len(found) >= 0.9, sorted(found - gold)
    assert len(found & gold) / len(gold) >= 0.85, sorted(gold - found)


def test_stage1_sql_is_valid_in_every_target_dialect(trap_db):
    snapshot = extract_duckdb(trap_db)
    for dialect in TARGET_DIALECTS:
        plan = prof.emit_stage1(snapshot, dialect, approx=True)
        assert len(plan["queries"]) == 13
        for query in plan["queries"]:
            sqlglot.parse_one(query["sql"], read=dialect if dialect != "duckdb" else None)
    tsql = prof.emit_stage1(snapshot, "tsql", approx=True)["queries"][0]["sql"]
    assert "APPROX_COUNT_DISTINCT(" in tsql and "APPROX_DISTINCT(" not in tsql


def test_text_columns_never_return_min_max():
    snapshot = Snapshot(datasets=[Dataset(name="people", columns=[Column("email", "varchar"), Column("age", "int")])])
    sql = prof.emit_stage1(snapshot, "duckdb")["queries"][0]["sql"]
    assert "MIN(age)" in sql and "MIN(email)" not in sql


def test_emit_ingest_round_trip_matches_local_run(trap_db):
    snapshot = extract_duckdb(trap_db)
    local = prof.run(trap_db, snapshot)
    plan1 = prof.emit_stage1(snapshot, "duckdb")
    results1 = {q["id"]: _rows(trap_db, q["sql"]) for q in plan1["queries"]}
    profile = prof.ingest(plan1, results1)
    plan2 = prof.emit_stage2(snapshot, profile, "duckdb", budget=10_000)
    profile = prof.ingest(plan2, {q["id"]: _rows(trap_db, q["sql"]) for q in plan2["queries"]}, profile)
    assert profile["datasets"] == local["datasets"]
    assert len(profile["inclusions"]) == len(local["inclusions"])
    assert profile["stage2"] is True


def test_ingest_reports_missing_results_and_never_stores_pii_values(trap_db):
    plan = {"stage": 2, "dialect": "duckdb", "queries": [
        {"id": "q1", "kind": "values", "dataset": "customers", "column": "email", "sql": "x"},
        {"id": "q2", "kind": "values", "dataset": "orders", "column": "status", "sql": "x"},
    ]}
    profile = prof.ingest(plan, {"q1": [{"value": "a@b.com", "n": 1}]})
    assert "values" not in profile["datasets"]["customers"]["columns"]["email"]
    assert profile["missing_results"] == ["q2"]
    assert "stage2" not in profile


def test_stage2_respects_budget(trap_db):
    snapshot = extract_duckdb(trap_db)
    profile = prof.ingest(prof.emit_stage1(snapshot, "duckdb"),
                          {q["id"]: _rows(trap_db, q["sql"]) for q in prof.emit_stage1(snapshot, "duckdb")["queries"]})
    plan = prof.emit_stage2(snapshot, profile, "snowflake", budget=5)
    assert len(plan["queries"]) == 5 and plan["truncated"] is True


def _rows(db, sql):
    from mautoontology.readonly import run_query

    return run_query(db, sql).as_dicts()
