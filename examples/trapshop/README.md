# Example: trapshop

A small web-shop warehouse built to contain the modelling traps that make LLM answers silently wrong, plus
the reviewed semantic layer mAutoOntology produced for it.

```bash
python build_warehouse.py trapshop.duckdb   # generates the DuckDB file (not committed)
```

## The traps

| Trap | Where | What the semantic layer does |
| :--- | :--- | :--- |
| No declared constraints | every table | keys and 14 relationships discovered from data evidence |
| Role-playing dimension | `orders.billing_address_id`, `orders.shipping_address_id` -> `addresses` | two relationships with roles; check-sql offers both |
| Many-to-many | `product_categories` | bridge entity + `many_to_many` relationship |
| Self references | `categories.parent_category_id`, `employees.manager_id` | self relationships with roles |
| SCD2 history | `customer_history` | `history: scd2`, current-version default filter |
| Soft deletes | `customers.is_deleted`, `orders.is_deleted` | default filters (MAO009 when missing) |
| Mixed currencies, minor units | `orders.amount_cents` + `orders.currency` | money with `currency_attr` and `scale: 0.01` (MAO010, MAO015) |
| Semi-additive snapshot | `account_balances` | `additivity: semi_additive` over `snapshot_date` (MAO011) |
| Fan-out | `order_items` | relationship direction (MAO006) |
| One-to-one | `shipments.order_id` | `cardinality: one_to_one` |
| Decoy key | `orders.promo_id` looks like `promos.promo_id`, but no values match | rejected, raised as a review question |
| Ambiguous "revenue" | gross vs net | two metrics with disambiguation + glossary term |

## How the layer was made

1. `mautoontology init --dialect duckdb --name trapshop --domain sales`
2. `mautoontology extract --duckdb trapshop.duckdb`
3. `mautoontology profile run --db trapshop.duckdb`
4. `mautoontology propose` (everything under `semantics/sales/*.yaml` except `metrics.yaml`)
5. Enrichment and review, as the skill instructs an agent + human to do:
   - descriptions and synonyms
   - approvals with `reviewed_by`
   - currencies for prices and balances
   - the `nordic` segment
   - `metrics.yaml`, `glossary.yaml` and `verified_queries/`
6. `mautoontology validate --strict`, `mautoontology index`, `mautoontology verify --db trapshop.duckdb`

Re-running `mautoontology propose` keeps every human edit; try it and look at `git diff`.

## Try it

```bash
mautoontology context --terms revenue --entities customer
mautoontology check-sql --sql "SELECT SUM(balance) FROM account_balances"
mautoontology query --db trapshop.duckdb --sql "SELECT currency, SUM(amount_cents) * 0.01 AS net_revenue FROM orders WHERE COALESCE(is_deleted, FALSE) = FALSE AND status NOT IN ('cancelled', 'returned') GROUP BY currency"
mautoontology export --to all --out exports
```
