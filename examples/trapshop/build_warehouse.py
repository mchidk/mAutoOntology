"""Build the "trap warehouse": a DuckDB database full of real-world modelling traps.

No constraints are declared, as in most Snowflake/Databricks warehouses, so
every key and relationship must be *discovered*. The gold relationships below
are known by construction and used to measure discovery precision/recall.

Traps:
* role-playing: orders.billing_address_id / shipping_address_id -> addresses
* bridge (many-to-many): product_categories(product_id, category_id)
* self references: categories.parent_category_id, employees.manager_id
* SCD2 history: customer_history(valid_from, valid_to, is_current)
* soft deletes: customers.is_deleted, orders.is_deleted
* multi-currency money stored in cents: orders.amount_cents + orders.currency
* semi-additive snapshot: account_balances(account_id, snapshot_date, balance)
* one-to-one: shipments.order_id (one shipment per shipped order)
* fan-out: order_items (many per order)
* decoys: orders.promo_id (values do NOT match promos), orders.tracking_id (no parent),
  products.category_code (a code, not a key)
"""

from __future__ import annotations

from pathlib import Path

GOLD_RELATIONSHIPS = {
    ("addresses", "customer_id", "customers", "customer_id"),
    ("orders", "customer_id", "customers", "customer_id"),
    ("orders", "billing_address_id", "addresses", "address_id"),
    ("orders", "shipping_address_id", "addresses", "address_id"),
    ("order_items", "order_id", "orders", "order_id"),
    ("order_items", "product_id", "products", "product_id"),
    ("categories", "parent_category_id", "categories", "category_id"),
    ("product_categories", "product_id", "products", "product_id"),
    ("product_categories", "category_id", "categories", "category_id"),
    ("employees", "manager_id", "employees", "employee_id"),
    ("accounts", "customer_id", "customers", "customer_id"),
    ("account_balances", "account_id", "accounts", "account_id"),
    ("shipments", "order_id", "orders", "order_id"),
    ("customer_history", "customer_id", "customers", "customer_id"),
}

DECOYS = {
    ("orders", "promo_id", "promos", "promo_id"),
}

SQL = """
CREATE TABLE customers AS
SELECT i AS customer_id,
       'user' || i || '@example.com' AS email,
       'Customer ' || i AS full_name,
       CASE i % 4 WHEN 0 THEN 'DK' WHEN 1 THEN 'DE' WHEN 2 THEN 'US' ELSE 'SE' END AS country_code,
       TIMESTAMP '2023-01-01 00:00:00' + INTERVAL (i) DAY AS created_at,
       (i % 25 = 0) AS is_deleted
FROM range(1, 401) t(i);

CREATE TABLE customer_history AS
SELECT row_number() OVER () AS customer_sk, customer_id, segment, valid_from, valid_to, valid_to IS NULL AS is_current
FROM (
  SELECT customer_id, 'standard' AS segment, DATE '2023-01-01' AS valid_from,
         CASE WHEN customer_id % 3 = 0 THEN DATE '2024-06-01' ELSE NULL END AS valid_to
  FROM customers
  UNION ALL
  SELECT customer_id, 'premium', DATE '2024-06-01', NULL FROM customers WHERE customer_id % 3 = 0
);

CREATE TABLE addresses AS
SELECT i AS address_id,
       ((i - 1) % 400) + 1 AS customer_id,
       CASE i % 5 WHEN 0 THEN 'Copenhagen' WHEN 1 THEN 'Berlin' WHEN 2 THEN 'Austin' WHEN 3 THEN 'Malmo' ELSE 'Aarhus' END AS city,
       lpad(CAST(1000 + i AS VARCHAR), 5, '0') AS postal_code
FROM range(1, 701) t(i);

CREATE TABLE promos AS
SELECT i AS promo_id, 'PROMO' || i AS promo_code FROM range(1, 6) t(i);

CREATE TABLE products AS
SELECT i AS product_id,
       'Product ' || i AS product_name,
       'CAT-' || (i % 7) AS category_code,
       CAST(5 + (i % 40) * 2.5 AS DECIMAL(10, 2)) AS list_price
FROM range(1, 121) t(i);

CREATE TABLE categories AS
SELECT i AS category_id,
       'Category ' || i AS category_name,
       CASE WHEN i <= 4 THEN NULL ELSE ((i - 1) % 4) + 1 END AS parent_category_id
FROM range(1, 21) t(i);

CREATE TABLE product_categories AS
SELECT p.product_id, c.category_id
FROM products p JOIN categories c ON (p.product_id + c.category_id) % 6 = 0;

CREATE TABLE orders AS
SELECT i AS order_id,
       ((i * 7) % 400) + 1 AS customer_id,
       ((i * 11) % 700) + 1 AS billing_address_id,
       ((i * 13) % 700) + 1 AS shipping_address_id,
       CASE i % 10 WHEN 0 THEN 'returned' WHEN 1 THEN 'cancelled' WHEN 2 THEN 'placed' ELSE 'shipped' END AS status,
       CASE i % 3 WHEN 0 THEN 'EUR' WHEN 1 THEN 'USD' ELSE 'DKK' END AS currency,
       CAST(1000 + (i * 37) % 50000 AS BIGINT) AS amount_cents,
       TIMESTAMP '2024-01-01 08:00:00' + INTERVAL (i * 3) HOUR AS ordered_at,
       CASE WHEN i % 4 = 0 THEN 100 + (i % 60) ELSE NULL END AS promo_id,
       'TRK' || lpad(CAST(i AS VARCHAR), 8, '0') AS tracking_id,
       (i % 50 = 0) AS is_deleted
FROM range(1, 3001) t(i);

CREATE TABLE order_items AS
SELECT o.order_id, l.line_no,
       ((o.order_id * 3 + l.line_no * 17) % 120) + 1 AS product_id,
       1 + (o.order_id + l.line_no) % 5 AS quantity,
       CAST(5 + ((o.order_id + l.line_no) % 40) * 2.5 AS DECIMAL(10, 2)) AS unit_price
FROM orders o, range(1, 4) l(line_no)
WHERE l.line_no <= 1 + o.order_id % 3;

CREATE TABLE shipments AS
SELECT row_number() OVER (ORDER BY order_id) + 50000 AS shipment_id, order_id,
       ordered_at + INTERVAL 2 DAY AS shipped_at
FROM orders WHERE status IN ('shipped', 'returned');

CREATE TABLE employees AS
SELECT i AS employee_id,
       CASE WHEN i <= 3 THEN NULL ELSE ((i - 1) % 3) + 1 END AS manager_id,
       'Employee ' || i AS full_name,
       'emp' || i || '@corp.example' AS work_email
FROM range(1, 61) t(i);

CREATE TABLE accounts AS
SELECT i AS account_id, ((i - 1) % 400) + 1 AS customer_id, 'ACC-' || i AS account_number
FROM range(1, 301) t(i);

CREATE TABLE account_balances AS
SELECT a.account_id, CAST(DATE '2024-01-01' + INTERVAL (d) DAY AS DATE) AS snapshot_date,
       CAST(1000 + (a.account_id * 13 + d * 7) % 5000 AS DECIMAL(12, 2)) AS balance
FROM accounts a, range(0, 30) t(d);
"""


def build(path: str | Path) -> Path:
    """Create (or recreate) the trap warehouse at ``path``."""
    import duckdb

    target = Path(path)
    if target.exists():
        target.unlink()
    con = duckdb.connect(str(target))
    try:
        con.execute(SQL)
    finally:
        con.close()
    return target


def build_tpch(path: str | Path, scale: float = 0.01) -> Path:
    """Create a TPC-H database with DuckDB's built-in generator (no constraints declared)."""
    import duckdb

    target = Path(path)
    if target.exists():
        target.unlink()
    con = duckdb.connect(str(target))
    try:
        con.execute(f"CALL dbgen(sf={scale})")
    finally:
        con.close()
    return target


#: TPC-H foreign keys (single-column) from the specification.
TPCH_GOLD = {
    ("nation", "n_regionkey", "region", "r_regionkey"),
    ("supplier", "s_nationkey", "nation", "n_nationkey"),
    ("customer", "c_nationkey", "nation", "n_nationkey"),
    ("partsupp", "ps_partkey", "part", "p_partkey"),
    ("partsupp", "ps_suppkey", "supplier", "s_suppkey"),
    ("orders", "o_custkey", "customer", "c_custkey"),
    ("lineitem", "l_orderkey", "orders", "o_orderkey"),
    ("lineitem", "l_partkey", "part", "p_partkey"),
    ("lineitem", "l_suppkey", "supplier", "s_suppkey"),
}


if __name__ == "__main__":
    import sys

    out = build(sys.argv[1] if len(sys.argv) > 1 else "trapshop.duckdb")
    print(f"Built {out}")
