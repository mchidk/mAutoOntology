-- PostgreSQL DDL
CREATE TABLE shop.customers (
  customer_id bigint PRIMARY KEY,
  email text,
  country_code char(2),
  created_at timestamp NOT NULL
);
COMMENT ON TABLE shop.customers IS 'One row per customer';

CREATE TABLE shop.products (
  product_id bigint PRIMARY KEY,
  name text,
  unit_price numeric(12,2)
);

CREATE TABLE shop.orders (
  order_id bigint PRIMARY KEY,
  customer_id bigint NOT NULL REFERENCES shop.customers (customer_id),
  status varchar(20),
  ordered_at timestamptz NOT NULL,
  amount numeric(12,2)
);

CREATE TABLE shop.order_items (
  order_id bigint NOT NULL REFERENCES shop.orders (order_id),
  line_no integer NOT NULL,
  product_id bigint NOT NULL REFERENCES shop.products (product_id),
  quantity integer,
  PRIMARY KEY (order_id, line_no)
);
