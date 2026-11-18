# check-sql rules and repair recipes

`mautoontology check-sql` exits 1 when any **error** remains (`--strict` also fails on warnings).
Fix errors; resolve or explicitly justify warnings in your answer.

| Rule | Severity | Problem | Repair |
| :--- | :--- | :--- | :--- |
| MAO001 | error | Not exactly one read-only SELECT (parse error, DML/DDL, several statements). | Submit one `SELECT`/`WITH` statement in the project dialect. |
| MAO002 | error | Table not in the semantic layer/catalog (warning if catalogued but not modelled). | Use a modelled dataset from INDEX.md; never guess table names. |
| MAO003 | error | Column missing or ambiguous. | Use the suggested column; qualify ambiguous names with the alias. |
| MAO004 | error | Join keys do not match a declared relationship, or no relationship exists. | Use the `on` columns from the hint (choose the right role), or join through the suggested path (bridges). |
| MAO005 | error | Cartesian product (no join predicate). | Add the relationship's ON condition. |
| MAO006 | error | Fan-out: a measure is aggregated after a to-many join, so values repeat. | Aggregate the many side in a CTE grouped by the parent key, then join; or use `COUNT(DISTINCT key)`. |
| MAO007 | error | Chasm trap: measures from two independent to-many branches. | One CTE per branch at the shared grain, then join the CTEs. |
| MAO008 | warning | Aggregation matches a metric but misses its filter, or is ambiguous between metrics. | Apply the metric's filter, or pick the metric the question means (see its disambiguation). |
| MAO009 | warning | Default filter missing (soft delete, SCD2 current version, as-of join). | Add the filter from the hint unless the question explicitly includes those rows; say so. |
| MAO010 | warning | Money summed across currencies. | `GROUP BY` the currency column, filter one currency, or convert with FX rates first. |
| MAO011 | warning | Semi-additive measure summed across its snapshot time. | Filter to one snapshot (e.g. latest) or group by the snapshot time; average across time. |
| MAO012 | error | Identifier aggregated with SUM/AVG. | Count with `COUNT(DISTINCT id)` or aggregate a real measure. |
| MAO013 | info/warning | Join uses a relationship that is not approved (warning when deprecated/rejected). | Mention it in the answer; prefer approved paths. |
| MAO014 | warning/info | Personal data in the output (explicit columns, `SELECT *`, value-collecting aggregates such as `STRING_AGG`, through CTEs and every UNION branch). | Return it only if the user is entitled to it; select explicit non-personal columns, aggregate or mask. |
| MAO015 | info | Measure stored in minor units. | Multiply by `scale` (e.g. `amount_cents * 0.01`). |
| MAO016 | warning | Non-additive measure (price, rate, ratio) summed. | Use `AVG`, or recompute from additive parts (e.g. `SUM(quantity * unit_price)`). |

## Fan-out example

Wrong (each order amount repeats once per order line):

```sql
SELECT SUM(o.amount_cents) FROM orders o JOIN order_items i ON i.order_id = o.order_id
```

Right:

```sql
WITH items AS (SELECT order_id, SUM(quantity) AS units FROM order_items GROUP BY order_id)
SELECT SUM(o.amount_cents) * 0.01 AS amount, SUM(i.units) AS units
FROM orders o LEFT JOIN items i ON i.order_id = o.order_id
WHERE COALESCE(o.is_deleted, FALSE) = FALSE
```

## How the check works

The SQL is parsed with sqlglot in the project dialect and analysed scope by scope (CTEs, subqueries,
every UNION branch; UNNEST/LATERAL helper scopes are skipped). Tables map to entities; join predicates
(`ON`, `USING`, `NATURAL` and `WHERE` equalities, including composite keys in any orientation) map to
declared relationships; the direction of each join (to-one vs to-many) decides fan-out. Column lineage is
tracked through CTEs and derived tables, so a fan-out hidden inside a CTE is still caught when the outer
query aggregates it, while a CTE that is aggregated before the join is (correctly) safe. An as-of join into
SCD2 history replaces the "current version" default filter.
