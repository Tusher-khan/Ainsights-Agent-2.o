"""SQL dialect differences the deterministic SQL builder has to respect.

Kept as pure functions (no drivers, no connections) so SQL generation is easy to
unit-test for every supported database.
"""

from __future__ import annotations

from app.utils.exceptions import UnsupportedRequestError

SUPPORTED = ("sqlite", "postgresql", "mysql", "sqlserver")


def period_expr(dialect: str, col: str, grain: str) -> str:
    """Expression that labels each row with its time bucket (sortable text)."""
    g = grain if grain in {"day", "week", "month", "quarter", "year"} else "month"
    if dialect == "postgresql":
        fmt = {"day": "YYYY-MM-DD", "week": 'IYYY-"W"IW', "month": "YYYY-MM", "quarter": 'YYYY-"Q"Q', "year": "YYYY"}[g]
        return f"to_char({col}, '{fmt}')"
    if dialect == "mysql":
        if g == "quarter":
            return f"CONCAT(YEAR({col}), '-Q', QUARTER({col}))"
        if g == "week":
            return f"DATE_FORMAT({col}, '%x-W%v')"
        fmt = {"day": "%Y-%m-%d", "month": "%Y-%m", "year": "%Y"}[g]
        return f"DATE_FORMAT({col}, '{fmt}')"
    if dialect == "sqlserver":
        if g == "quarter":
            return f"CONCAT(YEAR({col}), '-Q', DATEPART(quarter, {col}))"
        if g == "week":
            return f"CONCAT(YEAR({col}), '-W', RIGHT('0' + CAST(DATEPART(iso_week, {col}) AS VARCHAR(2)), 2))"
        fmt = {"day": "yyyy-MM-dd", "month": "yyyy-MM", "year": "yyyy"}[g]
        return f"FORMAT({col}, '{fmt}')"
    # sqlite
    if g == "quarter":
        return f"(strftime('%Y', {col}) || '-Q' || ((CAST(strftime('%m', {col}) AS INTEGER) + 2) / 3))"
    fmt = {"day": "%Y-%m-%d", "week": "%Y-W%W", "month": "%Y-%m", "year": "%Y"}[g]
    return f"strftime('{fmt}', {col})"


def agg_expr(dialect: str, aggregation: str, col: str) -> str:
    """Aggregate expression. Raises when a database cannot compute it exactly:
    AInsight never substitutes an approximation for a different statistic."""
    if aggregation == "sum":
        return f"SUM({col})"
    if aggregation in {"avg", "mean"}:
        return f"AVG({col} * 1.0)" if dialect in {"sqlserver"} else f"AVG({col})"
    if aggregation == "count":
        return f"COUNT({col})"
    if aggregation == "count_distinct":
        return f"COUNT(DISTINCT {col})"
    if aggregation == "min":
        return f"MIN({col})"
    if aggregation == "max":
        return f"MAX({col})"
    if aggregation == "median":
        if dialect == "postgresql":
            return f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {col})"
        raise UnsupportedRequestError(
            "Median cannot be computed exactly inside this type of database.",
            f"median unsupported for {dialect}",
        )
    if aggregation == "std":
        return {"postgresql": f"STDDEV_SAMP({col})", "mysql": f"STDDEV_SAMP({col})", "sqlserver": f"STDEV({col})"}.get(
            dialect
        ) or _unsupported("standard deviation", dialect)
    raise UnsupportedRequestError(f"Unsupported aggregation '{aggregation}'.")


def _unsupported(label: str, dialect: str):
    raise UnsupportedRequestError(f"{label.title()} cannot be computed exactly inside this type of database.", dialect)


def apply_limit(dialect: str, select_sql: str, limit: int | None) -> str:
    """Attach a row limit. SQL Server uses TOP and is handled in `top_clause`."""
    if not limit or dialect == "sqlserver":
        return select_sql
    return f"{select_sql} LIMIT {int(limit)}"


def top_clause(dialect: str, limit: int | None) -> str:
    return f"TOP {int(limit)} " if dialect == "sqlserver" and limit else ""
