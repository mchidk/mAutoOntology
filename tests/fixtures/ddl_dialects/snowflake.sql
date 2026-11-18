-- Snowflake DDL
CREATE TABLE shop.customers (
  customer_id NUMBER(38,0) NOT NULL PRIMARY KEY,
  email VARCHAR(255) COMMENT 'Customer e-mail',
  country_code VARCHAR(2),
  created_at TIMESTAMP_NTZ NOT NULL
) COMMENT = 'One row per customer';

CREATE TABLE shop.products (
  product_id NUMBER(38,0) NOT NULL,
  name VARCHAR,
  unit_price NUMBER(12,2),
  CONSTRAINT pk_products PRIMARY KEY (product_id)
);

CREATE TABLE shop.orders (
  order_id NUMBER(38,0) NOT NULL PRIMARY KEY,
  customer_id NUMBER(38,0) NOT NULL REFERENCES shop.customers (customer_id),
  status VARCHAR(20),
  ordered_at TIMESTAMP_TZ NOT NULL,
  amount NUMBER(12,2)
);

CREATE TABLE shop.order_items (
  order_id NUMBER(38,0) NOT NULL,
  line_no NUMBER(38,0) NOT NULL,
  product_id NUMBER(38,0) NOT NULL,
  quantity NUMBER(38,0),
  CONSTRAINT pk_order_items PRIMARY KEY (order_id, line_no)
);
ALTER TABLE shop.order_items ADD CONSTRAINT fk_items_order FOREIGN KEY (order_id) REFERENCES shop.orders (order_id);
ALTER TABLE shop.order_items ADD CONSTRAINT fk_items_product FOREIGN KEY (product_id) REFERENCES shop.products (product_id);
