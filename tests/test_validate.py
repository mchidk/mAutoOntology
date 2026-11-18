"""Validation: the reviewed example is clean; seeded mistakes are caught with stable codes."""

from __future__ import annotations

import json

import pytest

from mautoontology.model import Project
from mautoontology.snapshot import Snapshot
from mautoontology.validate import validate_project
from mautoontology.yamlio import dump_text, load_text

from .conftest import EXAMPLE


def _codes(root, **kwargs):
    report = validate_project(Project.load(root), **kwargs)
    return {i.code for i in report.issues}, report


def _edit(root, rel, fn):
    path = root / "semantics" / rel
    doc = load_text(path.read_text(encoding="utf-8"))
    fn(doc)
    path.write_text(dump_text(doc), encoding="utf-8")


def _attr(doc, attr_id):
    return next(a for a in doc["attributes"] if a["id"] == attr_id)


def test_reviewed_example_is_clean_even_in_strict_mode():
    report = validate_project(Project.load(EXAMPLE))
    assert report.ok
    assert report.counts() == {"error": 0, "warning": 0, "info": 0}, report.to_dict()["issues"]


@pytest.mark.parametrize(
    ("rel", "mutate", "code"),
    [
        ("sales/order.yaml", lambda d: d.__setitem__("bogus", 1), "V010"),
        ("sales/order.yaml", lambda d: _attr(d, "status").__setitem__("column", "state"), "V024"),
        ("sales/order.yaml", lambda d: _attr(d, "customer_id").__setitem__("aggregation", "sum"), "V025"),
        ("sales/account_balance.yaml", lambda d: _attr(d, "balance").pop("non_additive_over"), "V026"),
        ("sales/order.yaml", lambda d: _attr(d, "amount_cents")["money"].__setitem__("currency_attr", "ccy"), "V028"),
        ("sales/customer.yaml", lambda d: _attr(d, "email").__setitem__("values", ["a@b.com"]), "V029"),
        ("sales/order.yaml", lambda d: d["relationships"][0].__setitem__("to", "nowhere"), "V040"),
        ("sales/order.yaml", lambda d: d["relationships"][1]["on"][0].__setitem__("to", "country_code"), "V044"),
        ("sales/order.yaml", lambda d: d["default_filters"][0].__setitem__("sql", "is_deleted = = 1"), "V031"),
        ("sales/order.yaml", lambda d: d["relationships"][0].pop("reviewed_by"), "V050"),
    ],
    ids=["schema", "missing-column", "identifier-sum", "semi-additive-over", "currency-attr", "pii-values",
         "rel-target", "parent-not-key", "filter-parse", "approved-without-reviewer"],
)
def test_seeded_entity_mistakes(example_project, rel, mutate, code):
    _edit(example_project, rel, mutate)
    codes, _ = _codes(example_project)
    assert code in codes


def test_metric_glossary_and_query_mistakes(example_project):
    def metrics(doc):
        doc["metrics"][0]["expr"] = "amount * 0.01"
        doc["metrics"].append({"id": "loop_a", "type": "derived", "expr": "loop_b + 1", "inputs": ["loop_b"]})
        doc["metrics"].append({"id": "loop_b", "type": "derived", "expr": "loop_a + 1", "inputs": ["loop_a"]})
        doc["metrics"].append({"id": "bad_ratio", "type": "ratio", "numerator": "net_revenue", "denominator": "missing"})
        doc["metrics"].append({"id": "id_sum", "type": "simple", "entity": "order", "agg": "sum", "expr": "customer_id"})
        doc["metrics"].append({"id": "gross_sales_clone", "type": "simple", "entity": "order", "agg": "sum",
                               "expr": "amount_cents", "unit": "currency", "synonyms": ["gmv"]})
    _edit(example_project, "sales/metrics.yaml", metrics)
    _edit(example_project, "glossary.yaml", lambda d: d["terms"][1].__setitem__("maps_to", ["segment:customer.missing"]))
    _edit(example_project, "verified_queries/core.yaml", lambda d: d["queries"][0].__setitem__("sql", "SELECT SUM(customer_id) FROM orders"))
    codes, report = _codes(example_project)
    for code in ("V062", "V067", "V068", "V063", "V070", "V072", "V081"):
        assert code in codes, (code, report.to_dict()["issues"])
    assert not report.ok


def test_duplicate_ids_and_broken_yaml(example_project):
    src = example_project / "semantics" / "sales" / "order.yaml"
    (example_project / "semantics" / "sales" / "order_copy.yaml").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    (example_project / "semantics" / "broken.yaml").write_text("schema: [unclosed\n", encoding="utf-8")
    codes, _ = _codes(example_project)
    assert {"V001", "V002"} <= codes


def test_drift_against_a_new_snapshot(example_project):
    snapshot = Snapshot.load(example_project / "semantics" / "_evidence" / "snapshot.json")
    orders = snapshot.get("orders")
    orders.columns = [c for c in orders.columns if c.name != "promo_id"]
    orders.columns.append(type(orders.columns[0])(name="channel", data_type="VARCHAR"))
    status = orders.column("status")
    status.data_type, status.family = "INTEGER", "integer"
    snapshot.datasets = [d for d in snapshot.datasets if d.name != "promos"]
    fresh = example_project / "fresh.json"
    fresh.write_text(json.dumps(snapshot.to_dict()), encoding="utf-8")
    codes, _ = _codes(example_project, drift_snapshot=Snapshot.load(fresh))
    assert {"V090", "V091", "V092", "V093"} <= codes
