"""SQL security.

Generated SQL passes four independent gates before it can touch data:

1. **Lexical gate** — strips comments/strings, rejects statement stacking and
   any forbidden keyword.
2. **AST gate** — SQLGlot parses the statement; anything that is not a single
   SELECT (optionally with CTEs) is rejected, and every referenced table is
   checked against an allow-list.
3. **Authorizer gate** — the SQLite connection denies writes at engine level
   (see app/database/connection.py).
4. **Read-only connection** — the file is opened with `mode=ro`.

Gates 1 and 2 live here. Gate 2 degrades to a stricter lexical analysis when
SQLGlot is not installed, and this is reported by `validator_backend()`.
"""

from __future__ import annotations

import re

from app.utils.exceptions import SQLSecurityError
from app.utils.logger import get_logger

log = get_logger("security.sql")

try:  # pragma: no cover - depends on environment
    import sqlglot
    from sqlglot import exp

    SQLGLOT_AVAILABLE = True
except Exception:  # pragma: no cover
    sqlglot = None
    exp = None
    SQLGLOT_AVAILABLE = False

FORBIDDEN_KEYWORDS = [
    "drop", "delete", "update", "insert", "alter", "create", "truncate", "replace",
    "attach", "detach", "vacuum", "reindex", "grant", "revoke", "commit", "rollback",
    "savepoint", "pragma", "load_extension", "readfile", "writefile", "edit",
    "sqlite_master_writable", "exec", "execute", "shell", "system", "copy", "into outfile",
]

# Constructs that are valid inside a SELECT yet write data, lock rows, read server files,
# call out to other servers or stall the database. They matter once SQL runs against real
# business servers (PostgreSQL / MySQL / SQL Server), where `SELECT ... INTO new_table`
# creates a table and functions like pg_read_file or xp_cmdshell reach outside the database.
DANGEROUS_CONSTRUCTS = [
    "into", "lock", "pg_sleep", "sleep", "benchmark", "waitfor", "openrowset", "opendatasource",
    "openquery", "xp_cmdshell", "sp_executesql", "pg_read_file", "pg_read_binary_file", "pg_ls_dir",
    "lo_import", "lo_export", "dblink", "load_file", "outfile", "dumpfile", "get_lock", "set_config",
    "pg_terminate_backend", "pg_cancel_backend", "bulk", "kill", "shutdown", "declare",
]
FORBIDDEN_KEYWORDS = FORBIDDEN_KEYWORDS + DANGEROUS_CONSTRUCTS
FORBIDDEN_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in FORBIDDEN_KEYWORDS) + r")\b", re.I)
SQLGLOT_DIALECT = {"sqlite": "sqlite", "postgresql": "postgres", "mysql": "mysql", "sqlserver": "tsql"}
COMMENT_RE = re.compile(r"(--[^\n]*|/\*.*?\*/)", re.S)
STRING_RE = re.compile(r"'([^']|'')*'|\"([^\"]|\"\")*\"")
MAX_SQL_LENGTH = 6000


def validator_backend() -> str:
    return "sqlglot-ast" if SQLGLOT_AVAILABLE else "lexical-strict"


def _strip_literals(sql: str) -> str:
    """Remove comments and string literals so keyword scanning cannot be fooled."""
    without_comments = COMMENT_RE.sub(" ", sql)
    return STRING_RE.sub("''", without_comments)


def _lexical_gate(sql: str, dialect: str = "sqlite") -> str:
    if not sql or not sql.strip():
        raise SQLSecurityError("No SQL was produced for this question.")
    if len(sql) > MAX_SQL_LENGTH:
        raise SQLSecurityError("The generated query is too long to run safely.")

    # Comments are never needed by generated analytics SQL, and they are a classic way to
    # hide statements from a scanner (MySQL even *executes* /*! ... */ comments). Reject
    # them outright instead of stripping them for scanning only.
    no_strings = STRING_RE.sub("''", sql)
    if "--" in no_strings or "/*" in no_strings or "*/" in no_strings or (dialect == "mysql" and "#" in no_strings):
        raise SQLSecurityError("Comments are not allowed in analytical queries.", "comment marker found")
    if "\x00" in sql or (dialect == "mysql" and "\\" in sql):
        raise SQLSecurityError("That query contains characters that cannot be run safely.", "backslash/NUL")

    scannable = _strip_literals(sql)
    statements = [s for s in scannable.split(";") if s.strip()]
    if len(statements) > 1:
        raise SQLSecurityError(
            "Only a single SELECT statement can be run.", "statement stacking detected"
        )

    body = statements[0].strip()
    first_word = body.split()[0].lower() if body.split() else ""
    if first_word not in {"select", "with"}:
        raise SQLSecurityError(
            "AInsight only runs read-only SELECT queries.", f"statement starts with '{first_word}'"
        )

    match = FORBIDDEN_RE.search(body)
    if match:
        raise SQLSecurityError(
            "That query was blocked because it is not a read-only analytical query.",
            f"forbidden keyword '{match.group(1)}'",
        )
    if re.search(r"\bsqlite_(master|schema|temp_master)\b", body, re.I):
        raise SQLSecurityError("Queries against internal SQLite catalogues are not allowed.")
    return sql.strip().rstrip(";")


def _ast_gate(sql: str, allowed_tables: set[str] | None, dialect: str = "sqlite",
              allowed_schemas: set[str] | None = None) -> str:
    """Parse with SQLGlot and enforce single-SELECT plus table allow-list."""
    glot = SQLGLOT_DIALECT.get(dialect, "sqlite")
    try:
        expressions = sqlglot.parse(sql, read=glot)
    except Exception as exc:
        raise SQLSecurityError("The generated SQL could not be parsed.", str(exc)) from exc

    expressions = [e for e in expressions if e is not None]
    if len(expressions) != 1:
        raise SQLSecurityError("Only a single statement can be run.", f"{len(expressions)} statements")

    tree = expressions[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Subquery)) and not (
        isinstance(tree, exp.Expression) and tree.find(exp.Select) and tree.args.get("with")
    ):
        if not isinstance(tree, exp.Select):
            raise SQLSecurityError(
                "AInsight only runs SELECT queries.", f"root node {type(tree).__name__}"
            )

    unsafe_nodes = (
        exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
        exp.Command, exp.Pragma, exp.Attach, exp.Detach,
    )
    available = tuple(n for n in unsafe_nodes if n is not None)
    for node in tree.walk():
        if isinstance(node, available):
            raise SQLSecurityError(
                "That query was blocked because it is not read-only.",
                f"node {type(node).__name__}",
            )

    if allowed_tables is not None:
        cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        allowed = {t.lower() for t in allowed_tables} | cte_names
        for table in tree.find_all(exp.Table):
            name = (table.name or "").lower()
            schema = (table.db or "").lower()
            if schema and allowed_schemas is not None and schema not in {x.lower() for x in allowed_schemas}:
                raise SQLSecurityError(
                    f"The query references the schema '{table.db}', which is not available.",
                    "schema not in allow-list",
                )
            if name and name not in allowed:
                raise SQLSecurityError(
                    f"The query references '{table.name}', which is not an available table.",
                    "table not in allow-list",
                )
    return tree.sql(dialect=glot)


_IDENT = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*)'
_TABLE_REF_RE = re.compile(rf"^\s*({_IDENT})(?:\s*\.\s*({_IDENT}))?(?:\s*\.\s*({_IDENT}))?")
_FROM_END_RE = re.compile(r"\b(where|group|order|having|limit|union|intersect|except|offset|fetch|window)\b|\)", re.I)


def _unquote(identifier: str) -> str:
    return identifier.strip().strip('"`[]').lower()


def _lexical_table_gate(sql: str, allowed_tables: set[str] | None, allowed_schemas: set[str] | None = None) -> None:
    """Fallback table check when SQLGlot is missing. It understands comma joins,
    JOINs and schema-qualified names; anything it cannot classify is rejected."""
    if allowed_tables is None:
        return
    # Only single-quoted text is a string literal here: "double-quoted" and `backticked`
    # names are identifiers (table names) and must stay visible to the table check.
    body = re.sub(r"'([^']|'')*'", "''", sql)
    cte_names = {m.lower() for m in re.findall(r"\bwith\s+([A-Za-z_][\w]*)\s+as\b", body, re.I)}
    allowed = {t.lower() for t in allowed_tables} | cte_names
    schemas = {x.lower() for x in allowed_schemas} if allowed_schemas is not None else None
    unknown: set[str] = set()

    def check(fragment: str) -> None:
        fragment = fragment.strip()
        if not fragment or fragment.startswith("("):
            return  # sub-select: its own FROM is scanned separately
        match = _TABLE_REF_RE.match(fragment)
        if not match:
            unknown.add(fragment.split()[0] if fragment.split() else fragment)
            return
        parts = [_unquote(g) for g in match.groups() if g]
        name = parts[-1]
        if len(parts) > 1 and schemas is not None and parts[-2] not in schemas:
            unknown.add(".".join(parts))
        elif name not in allowed:
            unknown.add(name)

    for m in re.finditer(r"\bjoin\s+", body, re.I):
        check(body[m.end():])
    for m in re.finditer(r"\bfrom\s+", body, re.I):
        rest = body[m.end():]
        end = _FROM_END_RE.search(rest)
        segment = rest[: end.start()] if end else rest
        # cut at the first JOIN: later tables are handled by the JOIN scan above
        segment = re.split(r"\b(?:inner|left|right|full|cross|outer|natural)?\s*join\b", segment, flags=re.I)[0]
        depth, piece, pieces = 0, "", []
        for ch in segment:
            depth += ch == "("
            depth -= ch == ")"
            if ch == "," and depth == 0:
                pieces.append(piece)
                piece = ""
            else:
                piece += ch
        pieces.append(piece)
        for piece in pieces:
            check(piece)
    if unknown:
        raise SQLSecurityError(
            f"The query references unknown tables: {', '.join(sorted(unknown))}.",
            "table not in allow-list",
        )


def validate_sql(sql: str, allowed_tables: set[str] | None = None, dialect: str = "sqlite",
                 allowed_schemas: set[str] | None = None) -> str:
    """Validate and normalise generated SQL. Raises SQLSecurityError if unsafe."""
    checked = _lexical_gate(sql, dialect)
    if SQLGLOT_AVAILABLE:
        normalised = _ast_gate(checked, allowed_tables, dialect, allowed_schemas)
    else:
        _lexical_table_gate(checked, allowed_tables, allowed_schemas)
        normalised = checked
    log.info("SQL validated via %s", validator_backend())
    return normalised


def enforce_limit(sql: str, max_rows: int, dialect: str = "sqlite") -> str:
    """Append a LIMIT when the query does not already constrain its output.

    SQL Server has no LIMIT; its row cap is enforced by the connector fetching at most
    `max_rows + 1` rows, so the text is left alone."""
    if dialect == "sqlserver":
        return sql
    body = _strip_literals(sql)
    if re.search(r"\blimit\s+\d+", body, re.I):
        return sql
    return f"{sql.rstrip().rstrip(';')} LIMIT {int(max_rows)}"


def is_safe(sql: str, allowed_tables: set[str] | None = None, dialect: str = "sqlite") -> bool:
    try:
        validate_sql(sql, allowed_tables, dialect)
        return True
    except SQLSecurityError:
        return False
