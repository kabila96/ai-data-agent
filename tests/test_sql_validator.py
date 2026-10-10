"""SQL validation, injection and destructive-query rejection tests."""

from __future__ import annotations

import pytest

ACCEPTED = [
    "SELECT branch_id, SUM(revenue) FROM training.demo_sales GROUP BY 1",
    "select * from demo_sales where sale_date >= date '2026-08-01'",
    "WITH m AS (SELECT date_trunc('month', sale_date) AS m, SUM(revenue) r FROM training.demo_sales GROUP BY 1) "
    "SELECT m, r, lag(r) OVER (ORDER BY m) FROM m",
    "SELECT w.name, COUNT(f.*) FROM gis.wards w LEFT JOIN gis.health_facilities f "
    "ON ST_Contains(w.geom, f.geom) GROUP BY w.name",
    "SELECT ST_Area(ST_Transform(geom, 27700)) / 1e6 AS km2 FROM gis.wards",
    "SELECT * FROM training.demo_sales UNION ALL SELECT * FROM training.demo_sales",
    "SELECT order_id FROM orders",  # resolves to "Salesdata".orders (case preserved)
    "SELECT count(*) FILTER (WHERE revenue IS NULL) FROM training.demo_sales",
    "SELECT * FROM training.demo_sales WHERE currency = '$' ",
    "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY revenue) FROM training.demo_sales",
    "SELECT * FROM generate_series(1, 5) AS g",
    "SELECT to_char(sale_date, 'YYYY-MM') FROM training.demo_sales;",
]

REJECTED = {
    "drop": "DROP TABLE training.demo_sales",
    "delete": "DELETE FROM training.demo_sales",
    "update": "UPDATE training.demo_sales SET revenue = 0",
    "insert": "INSERT INTO training.demo_sales VALUES (1)",
    "truncate": "TRUNCATE training.demo_sales",
    "alter": "ALTER TABLE training.demo_sales ADD COLUMN x int",
    "create": "CREATE TABLE training.x AS SELECT 1",
    "grant": "GRANT SELECT ON training.demo_sales TO public",
    "revoke": "REVOKE SELECT ON training.demo_sales FROM public",
    "copy_to": "COPY training.demo_sales TO '/tmp/x.csv'",
    "copy_program": "COPY (SELECT 1) TO PROGRAM 'calc.exe'",
    "call": "CALL do_something()",
    "do_block": "DO $$ BEGIN PERFORM 1; END $$",
    "stacked": "SELECT 1 FROM training.demo_sales; DROP TABLE training.demo_sales",
    "stacked_select": "SELECT 1 FROM training.demo_sales; SELECT 2 FROM training.demo_sales",
    "line_comment": "SELECT * FROM training.demo_sales -- WHERE 1=0",
    "block_comment": "SELECT * FROM training.demo_sales /* ; DROP TABLE x */",
    "comment_smuggle": "SELECT 1 FROM training.demo_sales/**/;/**/DROP TABLE x",
    "dollar_quote": "SELECT $$x$$ FROM training.demo_sales",
    "tagged_dollar": "SELECT $a$; DROP TABLE x; $a$ FROM training.demo_sales",
    "select_into": "SELECT * INTO training.copy FROM training.demo_sales",
    "for_update": "SELECT * FROM training.demo_sales FOR UPDATE",
    "modifying_cte": "WITH d AS (DELETE FROM training.demo_sales RETURNING *) SELECT * FROM d",
    "pg_catalog": "SELECT * FROM pg_catalog.pg_authid",
    "pg_roles_unqualified": "SELECT rolname, rolpassword FROM pg_shadow",
    "information_schema": "SELECT * FROM information_schema.tables",
    "pg_sleep": "SELECT pg_sleep(30) FROM training.demo_sales",
    "pg_read_file": "SELECT pg_read_file('C:/Windows/win.ini')",
    "qualified_func": "SELECT pg_catalog.pg_sleep(1)",
    "set_config": "SELECT set_config('statement_timeout', '0', false)",
    "current_setting": "SELECT current_setting('data_directory')",
    "dblink": "SELECT * FROM dblink('host=evil', 'select 1') AS t(x int)",
    "lo_export": "SELECT lo_export(1234, '/tmp/x')",
    "nextval": "SELECT nextval('training.seq')",
    "unknown_func": "SELECT my_custom_side_effect() FROM training.demo_sales",
    "unapproved_table": "SELECT * FROM secret_hr.salaries",
    "ambiguous": "SELECT * FROM lookup",
    "cross_db": "SELECT * FROM otherdb.training.demo_sales",
    "set_role": "SET ROLE postgres",
    "reset_readonly": "SET default_transaction_read_only = off",
    "begin": "BEGIN; DELETE FROM training.demo_sales; COMMIT",
    "explain_analyze_delete": "EXPLAIN ANALYZE DELETE FROM training.demo_sales",
    "vacuum": "VACUUM training.demo_sales",
    "unicode_semicolon": "SELECT 1 FROM training.demo_sales\uff1b DROP TABLE x",
    "nul_byte": "SELECT 1 FROM training.demo_sales\x00; DROP TABLE x",
    "empty": "   ",
    "lock": "LOCK TABLE training.demo_sales",
    "notify": "SELECT pg_notify('c', 'x')",
    "advisory": "SELECT pg_advisory_lock(1)",
    "addgeometrycolumn": "SELECT AddGeometryColumn('gis','wards','g2',4326,'POINT',2)",
}


@pytest.mark.parametrize("sql", ACCEPTED)
def test_accepts_safe_selects(validator, sql):
    result = validator.validate(sql)
    assert result.ok, result.errors
    assert result.safe_sql and "LIMIT" in result.safe_sql.upper()


@pytest.mark.parametrize("name,sql", list(REJECTED.items()), ids=list(REJECTED))
def test_rejects_destructive_and_injection(validator, name, sql):
    result = validator.validate(sql)
    assert not result.ok, f"{name} was accepted: {result.safe_sql}"
    assert result.errors and result.safe_sql is None


def test_rejection_count_meets_requirement():
    assert len(REJECTED) >= 30


def test_tables_rewritten_fully_qualified(validator):
    result = validator.validate("select order_id from orders")
    assert '"Salesdata"."orders"' in result.safe_sql
    assert result.tables == ["Salesdata.orders"]


def test_row_cap_added_and_reduced(validator):
    assert validator.validate("SELECT * FROM training.demo_sales").safe_sql.endswith("LIMIT 1000")
    assert validator.validate("SELECT * FROM training.demo_sales LIMIT 99999").safe_sql.endswith("LIMIT 1000")
    assert validator.validate("SELECT * FROM training.demo_sales LIMIT 10").safe_sql.endswith("LIMIT 10")


def test_set_operations_wrapped_for_cap(validator):
    sql = validator.validate("SELECT 1 AS a FROM training.demo_sales UNION SELECT 2 FROM training.demo_sales").safe_sql
    assert sql.startswith("SELECT * FROM (") and sql.endswith("LIMIT 1000")


def test_non_literal_limit_wrapped(validator):
    sql = validator.validate("SELECT * FROM training.demo_sales LIMIT (SELECT 5)").safe_sql
    assert sql.endswith("LIMIT 1000") and "_capped" in sql


def test_cte_names_not_treated_as_tables(validator):
    result = validator.validate("WITH secret AS (SELECT 1 AS x FROM training.demo_sales) SELECT * FROM secret")
    assert result.ok


def test_bound_parameters_survive(validator):
    result = validator.validate("SELECT * FROM training.demo_sales WHERE branch_id IN :ids AND sale_date >= :start")
    assert result.ok, result.errors
    assert ":ids" in result.safe_sql and ":start" in result.safe_sql


def test_string_literal_with_keywords_is_data_not_code(validator):
    result = validator.validate("SELECT * FROM training.demo_sales WHERE currency = 'DROP TABLE x'")
    assert result.ok
    assert "'DROP TABLE x'" in result.safe_sql
