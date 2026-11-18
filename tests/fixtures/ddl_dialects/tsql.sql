-- SQL Server (T-SQL) DDL
CREATE TABLE shop.customers (
  customer_id BIGINT NOT NULL CONSTRAINT pk_customers PRIMARY KEY,
  email NVARCHAR(255) NULL,
  country_code NCHAR(2) NULL,
  created_at DATETIME2 NOT NULL
);
GO
CREATE TABLE shop.products (
  product_id BIGINT NOT NULL PRIMARY KEY,
  name NVARCHAR(200) NULL,
  unit_price DECIMAL(12,2) NULL
);
GO
CREATE TABLE shop.orders (
  order_id BIGINT NOT NULL PRIMARY KEY,
  customer_id BIGINT NOT NULL,
  status NVARCHAR(20) NULL,
  ordered_at DATETIMEOFFSET NOT NULL,
  amount DECIMAL(12,2) NULL,
  CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES shop.customers (customer_id)
);
GO
CREATE TABLE shop.order_items (
  order_id BIGINT NOT NULL,
  line_no INT NOT NULL,
  product_id BIGINT NOT NULL,
  quantity INT NULL,
  CONSTRAINT pk_order_items PRIMARY KEY (order_id, line_no),
  CONSTRAINT fk_items_order FOREIGN KEY (order_id) REFERENCES shop.orders (order_id),
  CONSTRAINT fk_items_product FOREIGN KEY (product_id) REFERENCES shop.products (product_id)
);
