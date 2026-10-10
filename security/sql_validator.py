"""Programmatic validation of model-generated SQL.

This is a defence-in-depth layer. The primary boundary is the restricted,
read-only PostgreSQL role (see `setup_readonly_user.sql`) plus read-only
transactions in `database.executor`. The validator rejects anything that is
not a single read-only SELECT over approved objects, and rewrites accepted
queries so they are schema-qualified and row-capped.

The validator works on sqlglot's token stream and abstract syntax tree; it
never relies on regular expressions over raw SQL text to decide that a query
is safe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError
from sqlglot.tokens import TokenType

from models.schemas import SQLValidationResult
from security.allowlists import (
    ALLOWED_ANONYMOUS_FUNCTIONS,
    ALWAYS_BLOCKED_SCHEMAS,
    is_denied_function,
)

logging.getLogger("sqlglot").setLevel(logging.ERROR)

MAX_SQL_LENGTH = 20_000
DIALECT = "postgres"

_FORBIDDEN_NODE_NAMES = (
    "Insert", "Update", "Delete", "Merge", "Drop", "Create", "Alter", "AlterTable",
    "TruncateTable", "Copy", "Grant", "Revoke", "Into", "Lock", "Command", "Set",
    "Transaction", "Commit", "Rollback", "Use", "Describe", "Pragma", "LoadData",
    "Analyze", "Kill", "Execute", "Refresh", "Cache", "Uncache", "Comment", "Summarize",
)
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, n) for n in _FORBIDDEN_NODE_NAMES if hasattr(exp, n)
)

_STRING_TOKENS = {
    getattr(TokenType, n)
    for n in ("STRING", "NATIONAL_STRING", "BYTE_STRING", "HEX_STRING", "BIT_STRING",
              "RAW_STRING", "UNICODE_STRING")
    if hasattr(TokenType, n)
}
_HEREDOC = getattr(TokenType, "HEREDOC_STRING", None)


@dataclass
class TableCatalog:
    """The set of objects the agent may read, keyed by lower-case `schema.table`.

    Values hold the real (case-preserved) schema and table names.
    """

    tables: dict[str, tuple[str, str]] = field(default_factory=dict)

    @classmethod
    def from_pairs(cls, pairs: list[tuple[str, str]]) -> "TableCatalog":
        return cls({f"{s.lower()}.{t.lower()}": (s, t) for s, t in pairs})

    def resolve(self, schema: str | None, table: str) -> tuple[str, str] | None | str:
        """Return (schema, table); None if unknown; 'ambiguous' if unqualified and not unique."""
        t = table.lower()
        if schema:
            return self.tables.get(f"{schema.lower()}.{t}")
        matches = [v for k, v in self.tables.items() if k.split(".", 1)[1] == t]
        if len(matches) == 1:
            return matches[0]
        return "ambiguous" if matches else None


class SQLValidator:
    def __init__(self, catalog: TableCatalog, max_rows: int, database_name: str | None = None) -> None:
        self.catalog = catalog
        self.max_rows = max_rows
        self.database_name = database_name

    # ------------------------------------------------------------------ API
    def validate(self, sql: str) -> SQLValidationResult:
        result = SQLValidationResult(ok=False, original_sql=sql if isinstance(sql, str) else "")
        try:
            ast = self._check(sql, result)
        except (ParseError, TokenError) as exc:
            result.errors.append(f"SQL could not be parsed: {str(exc).splitlines()[0][:200]}")
            return result
        if ast is None or result.errors:
            result.ok = False
            return result
        capped = self._apply_row_cap(ast)
        # Keep named bind parameters in SQLAlchemy's ":name" form.
        for placeholder in list(capped.find_all(exp.Placeholder)):
            name = placeholder.name
            if not name or not name.isidentifier():
                result.errors.append("Only named bind parameters (:name) are allowed.")
                return result
            placeholder.replace(exp.var(f":{name}"))
        result.safe_sql = capped.sql(dialect=DIALECT)
        result.row_cap = self.max_rows
        result.ok = True
        return result

    # -------------------------------------------------------------- checks
    def _check(self, sql: object, result: SQLValidationResult) -> exp.Expression | None:
        errors = result.errors
        if not isinstance(sql, str) or not sql.strip():
            errors.append("Empty query.")
            return None
        if len(sql) > MAX_SQL_LENGTH:
            errors.append(f"Query is longer than {MAX_SQL_LENGTH} characters.")
            return None
        if "\x00" in sql:
            errors.append("Query contains a NUL byte.")
            return None
        # Comments are rejected outright (not stripped) because they are a common
        # way of hiding a second statement or confusing simple filters.
        if "--" in sql or "/*" in sql or "*/" in sql:
            errors.append("SQL comments are not allowed.")
            return None

        tokens = sqlglot.tokenize(sql, read=DIALECT)
        if not tokens:
            errors.append("Empty query.")
            return None
        for i, tok in enumerate(tokens):
            if tok.comments:
                errors.append("SQL comments are not allowed.")
                return None
            if _HEREDOC is not None and tok.token_type == _HEREDOC:
                errors.append("Dollar-quoted strings are not allowed.")
                return None
            if tok.token_type in _STRING_TOKENS:
                continue
            if "$" in tok.text:
                errors.append("Dollar-quoting and positional parameters are not allowed.")
                return None
            if not tok.text.isascii():
                errors.append("Non-ASCII characters are only allowed inside string literals.")
                return None
            if tok.token_type == TokenType.SEMICOLON and i != len(tokens) - 1:
                errors.append("Only a single SQL statement is allowed.")
                return None
            if (
                tok.token_type in (TokenType.VAR, TokenType.IDENTIFIER)
                and i + 1 < len(tokens)
                and tokens[i + 1].token_type == TokenType.L_PAREN
                and is_denied_function(tok.text)
            ):
                errors.append(f"Function '{tok.text}' is not permitted.")
                return None

        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
        if len(statements) != 1:
            errors.append("Only a single SQL statement is allowed.")
            return None
        ast = statements[0]

        if not isinstance(ast, (exp.Select, exp.SetOperation)):
            errors.append(
                f"Only SELECT queries are allowed (received {type(ast).__name__.upper()})."
            )
            return None

        for node in ast.walk():
            if isinstance(node, FORBIDDEN_NODES):
                label = type(node).__name__.upper()
                if isinstance(node, exp.Into):
                    label = "SELECT INTO"
                elif isinstance(node, exp.Lock):
                    label = "FOR UPDATE/SHARE"
                errors.append(f"{label} is not allowed; queries must be read-only.")
                return None

        for check in (self._check_identifiers, self._check_functions):
            check(ast, errors)
            if errors:
                return None
        self._check_tables(ast, result)
        return None if errors else ast

    def _check_identifiers(self, ast: exp.Expression, errors: list[str]) -> None:
        for ident in ast.find_all(exp.Identifier):
            if ident.name.lower() in ALWAYS_BLOCKED_SCHEMAS:
                errors.append(
                    f"Access to '{ident.name}' is blocked. Use the Database Explorer "
                    "or the describe_table tool for metadata."
                )
                return

    def _check_functions(self, ast: exp.Expression, errors: list[str]) -> None:
        for func in ast.find_all(exp.Func):
            if isinstance(func, exp.Anonymous):
                name = func.name.lower()
                if is_denied_function(name):
                    errors.append(f"Function '{func.name}' is not permitted.")
                    return
                if name not in ALLOWED_ANONYMOUS_FUNCTIONS:
                    errors.append(
                        f"Function '{func.name}' is not on the approved function list."
                    )
                    return
                if isinstance(func.parent, exp.Dot):
                    errors.append("Schema-qualified function calls are not allowed.")
                    return
            else:
                name = func.sql_name().lower()
                if is_denied_function(name):
                    errors.append(f"Function '{name}' is not permitted.")
                    return

    def _check_tables(self, ast: exp.Expression, result: SQLValidationResult) -> None:
        cte_names = {cte.alias_or_name.lower() for cte in ast.find_all(exp.CTE)}
        referenced: list[str] = []
        for table in list(ast.find_all(exp.Table)):
            # Table-valued functions such as generate_series(...) or unnest(...).
            if not isinstance(table.this, exp.Identifier):
                continue
            schema = table.db or None
            catalog = table.catalog or None
            name = table.name
            if catalog and (not self.database_name or catalog.lower() != self.database_name.lower()):
                result.errors.append("Cross-database references are not allowed.")
                return
            if not schema and name.lower() in cte_names:
                continue
            if schema and schema.lower() in ALWAYS_BLOCKED_SCHEMAS:
                result.errors.append(f"Access to schema '{schema}' is blocked.")
                return
            resolved = self.catalog.resolve(schema, name)
            if resolved == "ambiguous":
                result.errors.append(
                    f"Table '{name}' exists in more than one approved schema; qualify it with a schema name."
                )
                return
            if resolved is None:
                target = f"{schema}.{name}" if schema else name
                result.errors.append(
                    f"'{target}' is not an approved table or view (or does not exist)."
                )
                return
            real_schema, real_table = resolved
            # Rewrite to a fully qualified reference so search_path cannot redirect it.
            table.set("db", exp.to_identifier(real_schema, quoted=True))
            table.set("this", exp.to_identifier(real_table, quoted=True))
            table.set("catalog", None)
            qualified = f"{real_schema}.{real_table}"
            if qualified not in referenced:
                referenced.append(qualified)
        if not referenced and not result.errors:
            result.warnings.append("Query does not read from any table.")
        result.tables = referenced

    # ------------------------------------------------------------ rewrite
    def _apply_row_cap(self, ast: exp.Expression) -> exp.Expression:
        cap = self.max_rows
        if isinstance(ast, exp.Select):
            limit = ast.args.get("limit")
            if limit is None:
                return ast.limit(cap)
            value = limit.expression
            if isinstance(value, exp.Literal) and value.is_int and int(value.this) <= cap:
                return ast
            if isinstance(value, exp.Literal) and value.is_int:
                return ast.limit(cap)
        # Set operations, or non-literal limits: wrap the whole query.
        return exp.select("*").from_(ast.subquery("_capped")).limit(cap)


def quote_ident(name: str) -> str:
    """Quote a PostgreSQL identifier (used for column names chosen in the UI)."""
    return '"' + name.replace('"', '""') + '"'
