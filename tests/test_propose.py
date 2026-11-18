"""Proposal: semantic typing, temporal patterns, idempotency, human-edit preservation, dbt adoption."""

from __future__ import annotations

import json
import shutil

import pytest

from mautoontology import profile as prof
from mautoontology.extract import extract_dbt, extract_duckdb
from mautoontology.model import Project
from mautoontology.propose.run import run_propose
from mautoontology.yamlio import dump_text, load_text

from .conftest import FIXTURES


@pytest.fixture()
def proposed(tmp_path, trap_db):
    root = tmp_path / "proj"
    snapshot = extract_duckdb(trap_db)
    snapshot.save(root / "semantics" / "_evidence" / "snapshot.json")
    prof.save_profile(prof.run(trap_db, snapshot), root / "semantics" / "_evidence" / "profile.json")
    summary = run_propose(root)
    return root, summary


def _entity(root, entity_id):
    return Project.load(root).entities[entity_id].raw


def _attr(raw, attr_id):
    return next(a for a in raw["attributes"] if a["id"] == attr_id)


def test_entity_types_grain_and_temporal_patterns(proposed):
    root, summary = proposed
    assert len(summary.entities_created) == 13
    assert _entity(root, "order")["entity_type"] == "fact"
    assert _entity(root, "product_category")["entity_type"] == "bridge"
    assert _entity(root, "order_item")["grain"] == ["order_id", "line_no"]
    history = _entity(root, "customer_history")
    assert history["entity_type"] == "history"
    assert history["history"]["type"] == "scd2"
    assert history["default_filters"][0]["reason"] == "scd2_current"
    snapshot = _entity(root, "account_balance")
    assert snapshot["entity_type"] == "snapshot"
    balance = _attr(snapshot, "balance")
    assert balance["additivity"] == "semi_additive" and balance["non_additive_over"] == "snapshot_date"
    assert _entity(root, "order")["default_filters"][0]["reason"] == "soft_delete"


def test_semantic_types_are_conservative(proposed):
    root, _ = proposed
    order = _entity(root, "order")
    amount = _attr(order, "amount_cents")
    assert amount["semantic_type"] == "money" and amount["scale"] == 0.01
    assert amount["money"] == {"currency_attr": "currency"}
    for attr in order["attributes"]:
        if attr["role"] == "identifier":
            assert attr.get("aggregation") not in {"sum", "avg"}
    unit_price = _attr(_entity(root, "order_item"), "unit_price")
    assert unit_price["additivity"] == "non_additive" and unit_price["aggregation"] == "avg"
    assert _attr(_entity(root, "order_item"), "line_no")["role"] != "measure"
    customer = _entity(root, "customer")
    assert _attr(customer, "email")["pii"] == "contact" and "values" not in _attr(customer, "email")
    assert _attr(customer, "country_code")["role"] == "dimension"
    assert set(_attr(order, "status")["values"]) == {"placed", "shipped", "returned", "cancelled"}


def test_relationships_and_many_to_many_through_bridge(proposed):
    root, _ = proposed
    project = Project.load(root)
    rels = {r.id: r for r in project.relationships()}
    assert rels["order__billing_address"].role == "billing"
    assert rels["shipment__order"].cardinality == "one_to_one"
    m2m = [r for r in rels.values() if r.cardinality == "many_to_many"]
    assert m2m and m2m[0].through == "product_category"
    assert rels["employee__manager_employee"].to_entity == "employee"  # self reference


def test_review_questions_flag_the_decoy(proposed):
    root, _ = proposed
    review = load_text((root / "semantics" / "_proposals" / "review.yaml").read_text(encoding="utf-8"))
    questions = " ".join(q["question"] for q in review["questions"])
    assert "orders.promo_id" in questions
    assert "accounts.customer_id" not in questions  # no noise about columns that found their parent


def test_rerun_is_idempotent(proposed):
    root, _ = proposed
    before = {p: p.read_bytes() for p in (root / "semantics").rglob("*.yaml") if "_proposals" not in p.parts}
    summary = run_propose(root)
    assert summary.entities_updated == [] and summary.conflicts == 0
    after = {p: p.read_bytes() for p in before}
    assert before == after


def test_human_edits_comments_and_deletions_survive(proposed):
    root, _ = proposed
    path = root / "semantics" / "core" / "order.yaml"
    doc = load_text(path.read_text(encoding="utf-8"))
    doc["label"] = "Customer order"
    rel = next(r for r in doc["relationships"] if r["id"] == "order__customer")
    rel["status"] = "approved"
    rel["reviewed_by"] = "finance"
    doc["attributes"] = [a for a in doc["attributes"] if a["id"] != "tracking_id"]  # human deletes an attribute
    text = "# Reviewed by finance\n" + dump_text(doc)
    path.write_bytes(text.encode("utf-8"))
    summary = run_propose(root)
    assert path.read_bytes() == text.encode("utf-8"), "a human-edited file was modified"
    assert summary.conflicts == 0  # approval, label and deletion are human decisions, not disagreements


def test_structural_disagreement_is_reported_as_conflict(proposed):
    root, _ = proposed
    path = root / "semantics" / "core" / "order.yaml"
    doc = load_text(path.read_text(encoding="utf-8"))
    rel = next(r for r in doc["relationships"] if r["id"] == "order__customer")
    rel["cardinality"] = "one_to_one"
    path.write_bytes(dump_text(doc).encode("utf-8"))
    summary = run_propose(root)
    assert summary.conflicts == 1
    review = load_text((root / "semantics" / "_proposals" / "review.yaml").read_text(encoding="utf-8"))
    assert review["conflicts"][0]["proposed"]["cardinality"] == {"current": "one_to_one", "proposed": "many_to_one"}


def test_untouched_elements_are_refreshed_when_evidence_changes(proposed):
    root, _ = proposed
    profile_path = root / "semantics" / "_evidence" / "profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["datasets"]["orders"]["columns"]["status"]["values"].append(["refunded", 1])
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    summary = run_propose(root)
    assert "order" in summary.entities_updated
    assert "refunded" in _attr(_entity(root, "order"), "status")["values"]


def test_dbt_semantic_layer_is_adopted_not_duplicated(tmp_path):
    root = tmp_path / "dbtproj"
    snapshot, _ = extract_dbt(FIXTURES / "jaffle_dbt")
    snapshot.save(root / "semantics" / "_evidence" / "snapshot.json")
    summary = run_propose(root)
    assert summary.adopted_metrics == 3
    project = Project.load(root)
    revenue = project.metrics["revenue"]
    assert revenue["origin"] == "dbt" and revenue["status"] == "approved"
    assert revenue["filter"] == "status = 'completed'"  # MetricFlow template converted to SQL
    assert revenue["entity"] == "order" and revenue["time"] == "ordered_at"
    assert project.metrics["average_order_value"]["type"] == "ratio"
    rels = project.relationships()
    assert rels[0].raw["evidence"][0]["kind"] == "dbt_test"
    # exporting to dbt again must not duplicate dbt-owned metrics
    from mautoontology.export.dbt import build

    doc, _ = build(project)
    assert "metrics" not in doc


def test_propose_without_snapshot_explains_next_step(tmp_path):
    with pytest.raises(FileNotFoundError, match="extract"):
        run_propose(tmp_path)


def test_example_layer_reproduces_from_scratch(tmp_path, trap_db):
    """The committed example's generated parts match what the toolkit produces today."""
    from .conftest import EXAMPLE

    root = tmp_path / "fresh"
    shutil.copytree(EXAMPLE / "semantics" / "_evidence", root / "semantics" / "_evidence")
    (root / "semantics" / "_evidence" / "generated.json").unlink()
    run_propose(root, domain="sales")
    fresh = Project.load(root)
    example = Project.load(EXAMPLE)
    assert set(fresh.entities) == set(example.entities)
    assert {r.id for r in fresh.relationships()} == {r.id for r in example.relationships()}
