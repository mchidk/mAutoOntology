"""Join graph and context: paths, warnings, SQL that actually runs and returns the verified answer."""

from __future__ import annotations

from mautoontology.checksql import check_sql
from mautoontology.context import build_context, render_markdown, search
from mautoontology.graph import JoinGraph
from mautoontology.index_md import build_index
from mautoontology.model import Project
from mautoontology.readonly import run_query

from .conftest import EXAMPLE


def project():
    return Project.load(EXAMPLE)


def test_shortest_path_prefers_to_one_and_routes_many_to_many_through_bridge():
    graph = JoinGraph(project())
    plan = graph.plan(["order", "customer"])
    assert [s.edge.rel.id for s in plan.steps] == ["order__customer"]
    assert plan.fanout_entities == []
    bridge = graph.plan(["product", "category"])
    assert [s.edge.dst for s in bridge.steps] == ["product_category", "category"]
    assert bridge.fanout_entities == ["product_category"]


def test_role_playing_and_unreachable_warnings():
    graph = JoinGraph(project())
    plan = graph.plan(["order", "address"])
    assert any("role-playing" in w for w in plan.warnings)
    assert graph.plan(["promo", "order"]).unreachable == ["order"]


def test_chasm_warning_only_for_diverging_branches():
    graph = JoinGraph(project())
    chain = graph.plan(["customer", "order", "order_item"])
    assert not any("chasm" in w for w in chain.warnings)  # nested chain, not a chasm
    via_role = graph.plan(["customer", "order", "address"])
    assert not any("chasm" in w for w in via_role.warnings)  # address is reached to-one through order
    chasm = graph.plan(["customer", "order", "customer_history"])
    assert any("chasm" in w for w in chasm.warnings)


def test_context_resolves_glossary_terms_and_builds_runnable_sql(example_project_ro):
    proj = Project.load(example_project_ro)
    ctx = build_context(proj, terms=["revenue"], entities=["customer"])
    assert {m["id"] for m in ctx["metrics"]} == {"net_revenue", "gross_revenue"}
    assert "AS order_" in ctx["from_sql"]  # 'order' is reserved: aliases are SQL-safe
    net = next(m for m in ctx["metrics"] if m["id"] == "net_revenue")
    filters = " AND ".join(f["sql"] for f in ctx["default_filters"])
    sql = f"SELECT customer.country_code, order_.currency, {net['sql']} AS net_revenue\n{ctx['from_sql']}\nWHERE {filters}\nGROUP BY 1, 2"
    check = check_sql(proj, sql, "duckdb")
    assert check.ok, [f.message for f in check.findings]
    rows = run_query(example_project_ro / "trapshop.duckdb", sql).rows
    reference = run_query(example_project_ro / "trapshop.duckdb",
                          "SELECT c.country_code, o.currency, SUM(o.amount_cents) * 0.01 FROM orders o JOIN customers c "
                          "ON o.customer_id = c.customer_id WHERE COALESCE(o.is_deleted, FALSE) = FALSE AND "
                          "COALESCE(c.is_deleted, FALSE) = FALSE AND o.status NOT IN ('cancelled', 'returned') GROUP BY 1, 2").rows
    assert sorted((a, b, round(float(v), 2)) for a, b, v in rows) == sorted((a, b, round(float(v), 2)) for a, b, v in reference)


def test_ratio_metric_sql_runs(example_project_ro):
    proj = Project.load(example_project_ro)
    ctx = build_context(proj, metrics=["average_order_value"])
    aov = ctx["metrics"][0]["sql"]
    rows = run_query(example_project_ro / "trapshop.duckdb",
                     f"SELECT order_.currency, {aov} {ctx['from_sql']} WHERE COALESCE(order_.is_deleted, FALSE) = FALSE GROUP BY 1").rows
    assert len(rows) == 3 and all(float(v) > 0 for _, v in rows)


def test_context_markdown_and_verified_queries():
    ctx = build_context(project(), metrics=["order_count"], entities=["customer"])
    md = render_markdown(ctx)
    assert "## Join path" in md and "## Default filters" in md and "orders_per_country" not in md
    assert any(q["id"] == "orders_per_country" for q in ctx["verified_queries"])


def test_lexical_search_finds_metrics_by_synonym():
    hits = [h["id"] for h in search(project(), "what is our turnover per market")[:3]]
    assert "net_revenue" in hits and "customer.country_code" in hits


def test_index_is_compact_and_deterministic():
    text = build_index(project())
    assert text == build_index(project())
    assert len(text) // 4 < 2000  # roughly tokens
    assert "`net_revenue`" in text and "role-playing" not in text
