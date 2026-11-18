-- Databricks (Unity Catalog) DDL; constraints are informational
CREATE TABLE shop.customers (
  customer_id BIGINT NOT NULL,
  email STRING COMMENT 'Customer e-mail',
  country_code STRING,
  created_at TIMESTAMP_NTZ NOT NULL,
  CONSTRAINT pk_customers PRIMARY KEY (customer_id)
) COMMENT 'One row per customer';

CREATE TABLE shop.products (
  product_id BIGINT NOT NULL,
  name STRING,
  unit_price DECIMAL(12,2),
  CONSTRAINT pk_products PRIMARY KEY (product_id)
);

CREATE TABLE shop.orders (
  order_id BIGINT NOT NULL,
  customer_id BIGINT NOT NULL,
  status STRING,
  ordered_at TIMESTAMP NOT NULL,
  amount DECIMAL(12,2),
  CONSTRAINT pk_orders PRIMARY KEY (order_id),
  CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES shop.customers (customer_id)
);

CREATE TABLE shop.order_items (
  order_id BIGINT NOT NULL,
  line_no INT NOT NULL,
  product_id BIGINT NOT NULL,
  quantity INT,
  CONSTRAINT pk_order_items PRIMARY KEY (order_id, line_no),
  CONSTRAINT fk_items_order FOREIGN KEY (order_id) REFERENCES shop.orders (order_id),
  CONSTRAINT fk_items_product FOREIGN KEY (product_id) REFERENCES shop.products (product_id)
);
