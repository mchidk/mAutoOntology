-- BigQuery DDL; keys are NOT ENFORCED (informational)
CREATE TABLE shop.customers (
  customer_id INT64 NOT NULL,
  email STRING OPTIONS(description='Customer e-mail'),
  country_code STRING,
  created_at DATETIME NOT NULL,
  PRIMARY KEY (customer_id) NOT ENFORCED
);
CREATE TABLE shop.products (
  product_id INT64 NOT NULL,
  name STRING,
  unit_price NUMERIC(12,2),
  PRIMARY KEY (product_id) NOT ENFORCED
);
CREATE TABLE shop.orders (
  order_id INT64 NOT NULL,
  customer_id INT64 NOT NULL,
  status STRING,
  ordered_at TIMESTAMP NOT NULL,
  amount NUMERIC(12,2),
  PRIMARY KEY (order_id) NOT ENFORCED,
  FOREIGN KEY (customer_id) REFERENCES shop.customers (customer_id) NOT ENFORCED
);
CREATE TABLE shop.order_items (
  order_id INT64 NOT NULL,
  line_no INT64 NOT NULL,
  product_id INT64 NOT NULL,
  quantity INT64,
  PRIMARY KEY (order_id, line_no) NOT ENFORCED,
  FOREIGN KEY (order_id) REFERENCES shop.orders (order_id) NOT ENFORCED,
  FOREIGN KEY (product_id) REFERENCES shop.products (product_id) NOT ENFORCED
);
