"""check-sql: a seeded corpus of wrong and right queries, each mapped to the rules it must (not) trigger."""

from __future__ import annotations

import pytest

from mautoontology.checksql import RULES, check_sql
from mautoontology.model import Project
from mautoontology.yamlio import load_file, plain

from .conftest import EXAMPLE, FIXTURES

CORPUS = plain(load_file(FIXTURES / "checksql_corpus.yaml"))["cases"]


@pytest.fixture(scope="module")
def project():
    return Project.load(EXAMPLE)


@pytest.mark.parametrize("case", CORPUS, ids=[c["id"] for c in CORPUS])
def test_corpus(project, case):
    result = check_sql(project, case["sql"], case.get("dialect", "duckdb"))
    rules = set(result.rules())
    for rule in case.get("expect", []):
        assert rule in rules, f"{case['id']}: expected {rule}, got {sorted(rules)}: {[f.message for f in result.findings]}"
    for rule in case.get("forbid", []):
        assert rule not in rules, f"{case['id']}: {rule} must not fire: {[f.message for f in result.findings if f.rule == rule]}"
    assert result.ok is case["ok"], f"{case['id']}: ok={result.ok}, findings={[(f.rule, f.message) for f in result.findings]}"


def test_corpus_covers_every_rule():
    covered = {rule for case in CORPUS for rule in case.get("expect", [])}
    assert covered >= set(RULES) - {"MAO013"}  # MAO013 is covered below (needs an unapproved relationship)


def test_unapproved_relationship_is_reported(example_project):
    path = example_project / "semantics" / "sales" / "order.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("status: approved", "status: proposed"), encoding="utf-8")
    result = check_sql(Project.load(example_project),
                       "SELECT c.country_code FROM orders o JOIN customers c ON o.customer_id = c.customer_id "
                       "WHERE COALESCE(o.is_deleted, FALSE) = FALSE AND COALESCE(c.is_deleted, FALSE) = FALSE")
    assert "MAO013" in result.rules()


def test_findings_carry_actionable_hints(project):
    result = check_sql(project, "SELECT a.city FROM orders o JOIN addresses a ON o.customer_id = a.address_id")
    join = next(f for f in result.findings if f.rule == "MAO004")
    assert "billing_address_id" in join.hint and "shipping_address_id" in join.hint  # both roles offered
    result = check_sql(project, "SELECT p.product_name FROM orders o JOIN products p ON o.order_id = p.product_id")
    assert "order_item" in next(f for f in result.findings if f.rule == "MAO004").hint  # path through the line items
    result = check_sql(project, "SELECT o.amount FROM orders o")
    assert "amount_cents" in next(f for f in result.findings if f.rule == "MAO003").hint


def test_verified_queries_are_clean(project):
    for query in project.queries.values():
        result = check_sql(project, query["sql"], query.get("dialect"))
        assert result.ok, (query["id"], [f.message for f in result.findings])
        assert not [f for f in result.findings if f.severity == "warning"], query["id"]


def test_result_serialises(project):
    data = check_sql(project, "SELECT SUM(customer_id) FROM orders").to_dict()
    assert data["ok"] is False
    assert data["findings"][0]["severity"] == "error"
    assert {"rule", "severity", "title", "message"} <= set(data["findings"][0])
