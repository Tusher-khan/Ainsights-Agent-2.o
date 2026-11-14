"""Text-to-SQL agent.

Two generation paths, one validation gate:

* `build_sql_from_plan`  — deterministic SQL assembled from the validated plan.
  Used in demo mode, as the fallback, and as the "no-LLM" baseline condition.
* `generate_sql_with_llm` — the model writes SQL grounded in the real schema.

Either way the SQL goes through `validate_query` (security + schema) before it
can execute, and a rejection is fed back to the model as a structured error for
a bounded number of retries.
"""

from __future__ import annotations

from config import Config
from app.ai import llm_provider
from app.ai.prompts import SQL_SYSTEM, sql_system_for, sql_user_prompt
from app.connectors import dialects
from app.core.datasource import ConnectedSQLSource, DataSource
from app.core.models import ExecutionResult, Filter, QueryPlan, ValidationReport
from app.database.query_executor import execute_sql
from app.database.query_validator import validate_query
from app.utils.exceptions import (
    ConnectorError, ConnectorQueryError, LLMError, QueryExecutionError, SQLSecurityError, UnsupportedRequestError,
)
from app.utils.helpers import dumps
from app.utils.logger import get_logger

log = get_logger("agents.sql")

_AGG_SQL = {
    "sum": 'SUM({col})',
    "avg": 'AVG({col})',
    "mean": 'AVG({col})',
    "count": 'COUNT({col})',
    "count_distinct": 'COUNT(DISTINCT {col})',
    "min": 'MIN({col})',
    "max": 'MAX({col})',
    "median": 'AVG({col})',   # SQLite has no MEDIAN
    "std": 'AVG({col})',
}

_GRAIN_SQL = {
    "day": "%Y-%m-%d",
    "week": "%Y-W%W",
    "month": "%Y-%m",
    "quarter": "%Y",   # refined below
    "year": "%Y",
}


def _quote(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def _literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _filter_sql(condition: Filter) -> str:
    column = _quote(condition.column)
    operator, value = condition.operator, condition.value
    if operator in {"=", "!=", ">", ">=", "<", "<="}:
        return f"{column} {operator} {_literal(value)}"
    if operator in {"in", "not_in"}:
        values = value if isinstance(value, (list, tuple)) else [value]
        joined = ", ".join(_literal(v) for v in values)
        return f"{column} {'NOT ' if operator == 'not_in' else ''}IN ({joined})"
    if operator == "contains":
        return f"{column} LIKE {_literal('%' + str(value) + '%')}"
    if operator == "not_contains":
        return f"{column} NOT LIKE {_literal('%' + str(value) + '%')}"
    if operator == "between":
        low, high = value if isinstance(value, (list, tuple)) and len(value) == 2 else (value, value)
        return f"{column} BETWEEN {_literal(low)} AND {_literal(high)}"
    if operator == "is_null":
        return f"{column} IS NULL"
    if operator == "not_null":
        return f"{column} IS NOT NULL"
    raise SQLSecurityError(f"Unsupported filter operator '{operator}'.")


def _alias(aggregation: str, measure: str) -> str:
    names = {"sum": "total", "avg": "average", "mean": "average", "count": "count",
             "count_distinct": "distinct", "min": "minimum", "max": "maximum",
             "median": "median", "std": "std_dev"}
    slug = "".join(ch if ch.isalnum() else "_" for ch in measure).strip("_").lower()
    return f"{names.get(aggregation, aggregation)}_{slug}"


# --------------------------------------------------------------------------
# Connected business databases (PostgreSQL / MySQL / SQL Server / uploaded SQLite)
# --------------------------------------------------------------------------


def _cquote(identifier: str, dialect: str) -> str:
    if dialect == "mysql":
        return "`" + str(identifier).replace("`", "``") + "`"
    return '"' + str(identifier).replace('"', '""') + '"'


def _cliteral(value, dialect: str, numeric: bool = False) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return ("TRUE" if value else "FALSE") if dialect == "postgresql" else ("1" if value else "0")
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    text = str(value)
    if numeric:
        try:
            return str(int(text)) if text.lstrip("-").isdigit() else repr(float(text))
        except ValueError:
            pass
    if "\x00" in text or (dialect == "mysql" and "\\" in text):
        raise UnsupportedRequestError(
            "That filter value contains characters AInsight cannot safely send to this database.",
            "backslash/NUL in literal",
        )
    return "'" + text.replace("'", "''") + "'"


def _cfilter(condition: Filter, source: ConnectedSQLSource) -> str:
    d = source.dialect
    column = _cquote(condition.column, d)
    profile = source.column_profile(condition.column) or {}
    numeric = profile.get("semantic_type") == "numeric"
    op, value = condition.operator, condition.value
    lit = lambda v: _cliteral(v, d, numeric)  # noqa: E731
    if op in {"=", "!=", ">", ">=", "<", "<="}:
        return f"{column} {op} {lit(value)}"
    if op in {"in", "not_in"}:
        values = value if isinstance(value, (list, tuple)) else [value]
        return f"{column} {'NOT ' if op == 'not_in' else ''}IN ({', '.join(lit(v) for v in values)})"
    if op in {"contains", "not_contains"}:
        pattern = _cliteral("%" + str(value).replace("%", "").replace("_", " ") + "%", d)
        return f"LOWER({column}) {'NOT ' if op == 'not_contains' else ''}LIKE LOWER({pattern})"
    if op == "between":
        low, high = value if isinstance(value, (list, tuple)) and len(value) == 2 else (value, value)
        return f"{column} BETWEEN {lit(low)} AND {lit(high)}"
    if op == "is_null":
        return f"{column} IS NULL"
    if op == "not_null":
        return f"{column} IS NOT NULL"
    raise SQLSecurityError(f"Unsupported filter operator '{op}'.")


def build_connected_sql(plan: QueryPlan, source: ConnectedSQLSource) -> str:
    """Deterministic SQL for a connected database, in that database's dialect."""
    d = source.dialect
    select_parts: list[str] = []
    group_parts: list[str] = []
    if plan.time_column and plan.time_grain:
        period = dialects.period_expr(d, _cquote(plan.time_column, d), plan.time_grain)
        select_parts.append(f"{period} AS period")
        group_parts.append(period)
    for dimension in plan.dimensions:
        select_parts.append(_cquote(dimension, d))
        group_parts.append(_cquote(dimension, d))
    value_alias = None
    if plan.measures:
        for measure in plan.measures:
            alias = _alias(plan.aggregation, measure)
            value_alias = value_alias or alias
            select_parts.append(f"{dialects.agg_expr(d, plan.aggregation, _cquote(measure, d))} AS {alias}")
    else:
        value_alias = "row_count"
        select_parts.append("COUNT(*) AS row_count")
    sql = f"SELECT {dialects.top_clause(d, plan.limit)}{', '.join(select_parts)} FROM {source.table_sql}"
    if plan.filters:
        sql += " WHERE " + " AND ".join(_cfilter(f, source) for f in plan.filters)
    if group_parts:
        sql += " GROUP BY " + ", ".join(group_parts)
    if plan.time_column and plan.time_grain:
        sql += " ORDER BY period ASC"
    elif plan.sort != "none" and value_alias:
        sql += f" ORDER BY {value_alias} {'ASC' if plan.sort == 'asc' else 'DESC'}"
    return dialects.apply_limit(d, sql, plan.limit)


def supports_aggregation(source: DataSource, aggregation: str) -> bool:
    """Can this source compute the aggregation exactly inside the database?"""
    if not isinstance(source, ConnectedSQLSource):
        return True
    try:
        dialects.agg_expr(source.dialect, aggregation, "x")
        return True
    except UnsupportedRequestError:
        return False


def build_sql_from_plan(plan: QueryPlan, source: DataSource) -> str:
    """Deterministically compose analytical SQL from a validated plan."""
    if isinstance(source, ConnectedSQLSource):
        return build_connected_sql(plan, source)
    table = _quote(source.table_name or source.name)
    select_parts: list[str] = []
    group_parts: list[str] = []

    if plan.time_column and plan.time_grain:
        fmt = _GRAIN_SQL.get(plan.time_grain, "%Y-%m")
        period = f"strftime('{fmt}', {_quote(plan.time_column)})"
        select_parts.append(f"{period} AS period")
        group_parts.append(period)

    for dimension in plan.dimensions:
        select_parts.append(_quote(dimension))
        group_parts.append(_quote(dimension))

    value_alias = None
    if plan.measures:
        for measure in plan.measures:
            template = _AGG_SQL.get(plan.aggregation, "SUM({col})")
            alias = _alias(plan.aggregation, measure)
            value_alias = value_alias or alias
            select_parts.append(f"{template.format(col=_quote(measure))} AS {alias}")
    else:
        value_alias = "row_count"
        select_parts.append("COUNT(*) AS row_count")

    sql = f"SELECT {', '.join(select_parts)} FROM {table}"
    if plan.filters:
        sql += " WHERE " + " AND ".join(_filter_sql(f) for f in plan.filters)
    if group_parts:
        sql += " GROUP BY " + ", ".join(group_parts)
    if plan.time_column and plan.time_grain:
        sql += " ORDER BY period ASC"
    elif plan.sort != "none" and value_alias:
        sql += f" ORDER BY {value_alias} {'ASC' if plan.sort == 'asc' else 'DESC'}"
    if plan.limit:
        sql += f" LIMIT {int(plan.limit)}"
    return sql


def generate_sql_with_llm(question: str, plan: QueryPlan, source: DataSource, error: str = "") -> tuple[str, str]:
    """Return (sql, method). Falls back to the deterministic builder."""
    system = sql_system_for(source.dialect, source.table_sql) if isinstance(source, ConnectedSQLSource) else SQL_SYSTEM
    try:
        response = llm_provider.complete(
            system,
            sql_user_prompt(question, source.schema_text(), dumps(plan.model_dump()), error),
            json_mode=True,
        )
    except LLMError as exc:
        log.warning("SQL generation LLM call failed (%s); using deterministic builder", exc.detail)
        return build_sql_from_plan(plan, source), "deterministic"

    payload = response.json()
    if response.from_demo or not isinstance(payload, dict) or payload.get("use_deterministic_fallback"):
        return build_sql_from_plan(plan, source), "deterministic"
    sql = str(payload.get("sql", "")).strip()
    if not sql:
        return build_sql_from_plan(plan, source), "deterministic"
    return sql, "llm"


def _run_connected(question: str, plan: QueryPlan, source: ConnectedSQLSource) -> tuple[ExecutionResult, ValidationReport, dict]:
    """Same generate → validate → execute loop, against a user-connected database.

    The bounded retry only repairs *query* problems (rejected or failed SQL). A broken
    connection or bad credentials are reported immediately instead of being retried.
    """
    from app.services import connection_service

    allowed = {source.table_name}
    schemas = {source.schema} if source.schema else None
    attempts: list[dict] = []
    error_feedback = ""
    max_attempts = max(1, Config.MAX_QUERY_RETRIES + 1)
    for attempt in range(max_attempts):
        sql, method = generate_sql_with_llm(question, plan, source, error_feedback)
        try:
            safe_sql, report = validate_query(
                sql, allowed, Config.MAX_RESULT_ROWS, dialect=source.dialect,
                known_columns=set(source.columns), allowed_schemas=schemas,
            )
        except SQLSecurityError as exc:
            attempts.append({"attempt": attempt + 1, "method": method, "sql": sql, "error": exc.detail})
            error_feedback = exc.detail
            if method == "deterministic":
                raise
            continue
        try:
            result = connection_service.execute_for_source(source, safe_sql)
        except ConnectorQueryError as exc:
            attempts.append({"attempt": attempt + 1, "method": method, "sql": safe_sql, "error": exc.detail})
            error_feedback = exc.detail
            if method == "deterministic":
                raise QueryExecutionError(exc.user_message, exc.detail) from exc
            continue
        except ConnectorError:
            raise
        attempts.append({"attempt": attempt + 1, "method": method, "sql": safe_sql, "error": None})
        return result, report, {"attempts": attempts, "retries": attempt, "method": method}
    raise QueryExecutionError(
        "AInsight could not produce a valid query for that question.",
        f"all {max_attempts} SQL attempts failed: {attempts}",
    )


def run(question: str, plan: QueryPlan, source: DataSource) -> tuple[ExecutionResult, ValidationReport, dict]:
    """Generate, validate (with retries) and execute SQL."""
    if isinstance(source, ConnectedSQLSource):
        return _run_connected(question, plan, source)
    allowed = {source.table_name or source.name}
    attempts: list[dict] = []
    error_feedback = ""
    max_attempts = max(1, Config.MAX_QUERY_RETRIES + 1)

    for attempt in range(max_attempts):
        sql, method = generate_sql_with_llm(question, plan, source, error_feedback)
        try:
            safe_sql, report = validate_query(sql, allowed, Config.MAX_RESULT_ROWS)
        except SQLSecurityError as exc:
            attempts.append({"attempt": attempt + 1, "method": method, "sql": sql, "error": exc.detail})
            error_feedback = exc.detail
            log.warning("SQL attempt %s rejected: %s", attempt + 1, exc.detail)
            if method == "deterministic":
                raise
            continue

        try:
            result = execute_sql(safe_sql)
        except QueryExecutionError as exc:
            attempts.append({"attempt": attempt + 1, "method": method, "sql": safe_sql, "error": exc.detail})
            error_feedback = exc.detail
            log.warning("SQL attempt %s failed to execute: %s", attempt + 1, exc.detail)
            if method == "deterministic":
                raise
            continue

        attempts.append({"attempt": attempt + 1, "method": method, "sql": safe_sql, "error": None})
        result.operation = safe_sql
        return result, report, {"attempts": attempts, "retries": attempt, "method": method}

    raise QueryExecutionError(
        "AInsight could not produce a valid query for that question.",
        f"all {max_attempts} SQL attempts failed: {attempts}",
    )
