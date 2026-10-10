-- =============================================================================
-- demo_seed_sales.sql (optional demonstration data)
-- Creates deterministic retail demo tables in the existing `training` schema:
--   training.demo_branches, training.demo_products, training.demo_sales, training.demo_stock
-- Includes deliberate data-quality issues so profiling and anomaly checks have
-- something to find: NULL revenue, duplicate rows, a negative quantity, an extreme
-- transaction, a mixed-currency batch and one product in steady decline.
--
-- Run as an administrator (NOT as powell_agent_ro):
--   psql -U postgres -d powell_analytics -f sample_data/demo_seed_sales.sql
-- Then re-run setup_readonly_user.sql (or rely on its default privileges) so the
-- agent role can read the new tables.
-- =============================================================================
\set ON_ERROR_STOP on
BEGIN;

CREATE SCHEMA IF NOT EXISTS training;

DROP TABLE IF EXISTS training.demo_stock, training.demo_sales,
                     training.demo_products, training.demo_branches;

CREATE TABLE training.demo_branches (
    branch_id   integer PRIMARY KEY,
    branch_name text NOT NULL,
    region      text NOT NULL,
    city        text NOT NULL,
    opened_on   date NOT NULL
);
COMMENT ON TABLE training.demo_branches IS 'Demo retail branches (synthetic).';

INSERT INTO training.demo_branches VALUES
 (1, 'Camden',       'London',        'London',     '2019-03-01'),
 (2, 'Canary Wharf', 'London',        'London',     '2020-06-15'),
 (3, 'Digbeth',      'West Midlands', 'Birmingham', '2018-09-01'),
 (4, 'Edgbaston',    'West Midlands', 'Birmingham', '2021-02-01'),
 (5, 'Northern Quarter', 'North West', 'Manchester', '2022-05-01');

CREATE TABLE training.demo_products (
    product_id   integer PRIMARY KEY,
    product_name text NOT NULL,
    category     text NOT NULL,
    unit_cost    numeric(10,2) NOT NULL,
    unit_price   numeric(10,2) NOT NULL
);
COMMENT ON TABLE training.demo_products IS 'Demo product catalogue (synthetic). Prices in GBP.';

INSERT INTO training.demo_products VALUES
 (101, 'Arabica Beans 1kg',  'Coffee',      9.50, 18.00),
 (102, 'Espresso Pods x20',  'Coffee',      3.10,  6.50),
 (103, 'Oat Milk 1L',        'Dairy Alt',   0.95,  2.20),
 (104, 'Almond Milk 1L',     'Dairy Alt',   1.05,  2.40),
 (105, 'Croissant',          'Bakery',      0.40,  1.80),
 (106, 'Sourdough Loaf',     'Bakery',      1.20,  4.20),
 (107, 'Green Tea x40',      'Tea',         1.60,  4.00),
 (108, 'Earl Grey x40',      'Tea',         1.40,  3.80),
 (109, 'Reusable Cup',       'Merchandise', 2.50,  9.00),
 (110, 'Cafetiere',          'Merchandise', 8.00, 24.00),
 (111, 'Chai Syrup',         'Syrups',      2.20,  5.50),
 (112, 'Vanilla Syrup',      'Syrups',      2.00,  5.00);

CREATE TABLE training.demo_sales (
    sale_id     bigint PRIMARY KEY,
    sale_date   date NOT NULL,
    branch_id   integer NOT NULL REFERENCES training.demo_branches,
    product_id  integer NOT NULL REFERENCES training.demo_products,
    quantity    integer NOT NULL,
    unit_price  numeric(10,2) NOT NULL,
    revenue     numeric(12,2),
    cost        numeric(12,2),
    currency    text NOT NULL DEFAULT 'GBP'
);
COMMENT ON TABLE training.demo_sales IS 'Daily sales by branch and product (synthetic). revenue = quantity * unit_price.';
COMMENT ON COLUMN training.demo_sales.revenue IS 'Gross revenue in the row currency';
COMMENT ON COLUMN training.demo_sales.cost IS 'Cost of goods sold in the row currency';

SELECT setseed(0.42);

INSERT INTO training.demo_sales (sale_id, sale_date, branch_id, product_id, quantity, unit_price, revenue, cost, currency)
SELECT row_number() OVER (ORDER BY d, b.branch_id, p.product_id) AS sale_id,
       d::date, b.branch_id, p.product_id, q.qty, p.unit_price,
       q.qty * p.unit_price, q.qty * p.unit_cost, 'GBP'
FROM generate_series(DATE '2025-04-01', DATE '2026-09-30', INTERVAL '1 day') AS d
CROSS JOIN training.demo_branches b
CROSS JOIN training.demo_products p
CROSS JOIN LATERAL (
    SELECT GREATEST(0, round(
        (8 + b.branch_id * 1.5)                                         -- branch size
        * (1 + 0.25 * sin(2 * pi() * extract(doy FROM d) / 365.0))       -- seasonality
        * (CASE WHEN extract(isodow FROM d) IN (6, 7) THEN 1.3 ELSE 1 END) -- weekends
        * (CASE WHEN p.product_id = 108                                  -- declining product
                THEN GREATEST(0.15, 1 - (d::date - DATE '2025-04-01') / 600.0) ELSE 1 END)
        * (CASE WHEN p.category = 'Merchandise' THEN 0.2 ELSE 1 END)
        * (0.75 + random() * 0.5)
    ))::int AS qty
) q
WHERE d::date >= b.opened_on;

-- Data-quality issues (deliberate) ------------------------------------------------
UPDATE training.demo_sales SET revenue = NULL WHERE sale_id % 997 = 0;                  -- missing revenue
UPDATE training.demo_sales SET quantity = -quantity, revenue = -revenue WHERE sale_id = 5001; -- negative quantity
UPDATE training.demo_sales SET quantity = quantity * 40, revenue = revenue * 40,
                               cost = cost * 40 WHERE sale_id = 12345;                 -- extreme transaction
UPDATE training.demo_sales SET currency = 'USD', unit_price = unit_price * 1.27,
       revenue = revenue * 1.27, cost = cost * 1.27
WHERE branch_id = 2 AND sale_date BETWEEN DATE '2026-02-01' AND DATE '2026-02-07';        -- mixed currency
INSERT INTO training.demo_sales                                                          -- duplicate rows
SELECT sale_id + 1000000, sale_date, branch_id, product_id, quantity, unit_price, revenue, cost, currency
FROM training.demo_sales WHERE sale_id BETWEEN 20000 AND 20009;

CREATE INDEX ON training.demo_sales (sale_date);
CREATE INDEX ON training.demo_sales (branch_id, product_id);

CREATE TABLE training.demo_stock (
    snapshot_date date NOT NULL,
    branch_id     integer NOT NULL REFERENCES training.demo_branches,
    product_id    integer NOT NULL REFERENCES training.demo_products,
    stock_on_hand integer NOT NULL,
    stockout      boolean NOT NULL,
    PRIMARY KEY (snapshot_date, branch_id, product_id)
);
COMMENT ON TABLE training.demo_stock IS 'Weekly stock snapshots (synthetic). stockout = no stock on hand.';

INSERT INTO training.demo_stock
SELECT d::date, b.branch_id, p.product_id, s.qty, s.qty = 0
FROM generate_series(DATE '2025-04-07', DATE '2026-09-28', INTERVAL '7 days') AS d
CROSS JOIN training.demo_branches b
CROSS JOIN training.demo_products p
CROSS JOIN LATERAL (
    SELECT CASE WHEN random() < 0.04 THEN 0 ELSE (5 + random() * 80)::int END AS qty
) s
WHERE d::date >= b.opened_on;

ANALYZE training.demo_branches, training.demo_products, training.demo_sales, training.demo_stock;
COMMIT;

SELECT 'demo_sales' AS table_name, count(*) FROM training.demo_sales
UNION ALL SELECT 'demo_stock', count(*) FROM training.demo_stock;
