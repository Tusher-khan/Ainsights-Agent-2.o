"""Business semantic layer.

A *metric* is a named business definition such as

    Profit Margin = SUM(profit) / SUM(sales)
    Net Revenue   = SUM(gross_revenue) - SUM(returns) - SUM(discounts)

Definitions are parsed by a tiny arithmetic grammar — aggregates over real columns,
numbers, + - * / and parentheses — never evaluated as code. The same parsed definition
is executed either in the user's database (translated to SQL in that database's dialect)
or with pandas, so "Profit Margin by region" means the same thing on every source.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

from config import Config
from app.connectors import dialects
from app.core.datasource import ConnectedSQLSource, DataSource, FrameDataSource
from app.core.models import ExecutionResult, QueryPlan
from app.utils.exceptions import AInsightError
from app.utils.helpers import dataframe_to_records, json_safe, slugify


class MetricError(AInsightError):
    status_code = 422
    user_message = "That metric definition is not valid."


AGGS = {"SUM": "sum", "AVG": "avg", "MIN": "min", "MAX": "max", "COUNT": "count", "COUNT_DISTINCT": "count_distinct"}
NUMERIC_AGGS = {"sum", "avg", "min", "max"}
_TOKEN = re.compile(r'\s*(?:(\d+(?:\.\d+)?)|([A-Za-z_][A-Za-z_0-9]*)|"([^"]+)"|\[([^\]]+)\]|(.))')
MAX_EXPR_LEN = 300


@dataclass
class Node:
    kind: str                     # num | agg | bin | neg
    value: float | None = None
    agg: str = ""
    column: str = ""
    op: str = ""
    left: "Node | None" = None
    right: "Node | None" = None


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens, pos = [], 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise MetricError("The definition contains something that cannot be read.", text[pos:pos + 10])
        pos = m.end()
        num, ident, quoted, bracket, sym = m.groups()
        if num is not None:
            tokens.append(("num", num))
        elif ident is not None:
            tokens.append(("id", ident))
        elif quoted is not None or bracket is not None:
            tokens.append(("col", quoted if quoted is not None else bracket))
        elif sym is not None and not sym.isspace():
            tokens.append(("sym", sym))
    return tokens


def parse(expression: str) -> Node:
    if not expression or len(expression) > MAX_EXPR_LEN:
        raise MetricError(f"A definition must be between 1 and {MAX_EXPR_LEN} characters.")
    tokens = _tokenize(expression)
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else (None, None)

    def take():
        nonlocal pos
        tok = tokens[pos]
        pos += 1
        return tok

    def expr() -> Node:
        node = term()
        while peek() in (("sym", "+"), ("sym", "-")):
            op = take()[1]
            node = Node("bin", op=op, left=node, right=term())
        return node

    def term() -> Node:
        node = factor()
        while peek() in (("sym", "*"), ("sym", "/")):
            op = take()[1]
            node = Node("bin", op=op, left=node, right=factor())
        return node

    def factor() -> Node:
        kind, value = peek()
        if kind is None:
            raise MetricError("The definition ends unexpectedly.")
        if kind == "num":
            take()
            return Node("num", value=float(value))
        if (kind, value) == ("sym", "-"):
            take()
            return Node("neg", left=factor())
        if (kind, value) == ("sym", "("):
            take()
            inner = expr()
            if peek() != ("sym", ")"):
                raise MetricError("A closing bracket is missing.")
            take()
            return inner
        if kind == "id" and value.upper() in AGGS:
            take()
            if peek() != ("sym", "("):
                raise MetricError(f"Write {value.upper()}(column).")
            take()
            ckind, cval = peek()
            if ckind not in {"id", "col"}:
                raise MetricError(f"{value.upper()} needs a column name inside the brackets.")
            take()
            if peek() != ("sym", ")"):
                raise MetricError("A closing bracket is missing.")
            take()
            return Node("agg", agg=AGGS[value.upper()], column=cval)
        raise MetricError(
            "Definitions can only use SUM, AVG, MIN, MAX, COUNT or COUNT_DISTINCT of a column, numbers, + - * / and brackets.",
            f"unexpected {value!r}",
        )

    tree = expr()
    if pos != len(tokens):
        raise MetricError("The definition has extra text at the end.", f"left over: {tokens[pos:]}")
    return tree


def resolve(tree: Node, source: DataSource) -> Node:
    """Check every column exists (and is numeric where required); return a tree with real column names."""
    if tree.kind == "agg":
        column = source.resolve_column(tree.column)
        if not column:
            raise MetricError(f"The column '{tree.column}' does not exist in this dataset.")
        if tree.agg in NUMERIC_AGGS and column not in source.measures:
            raise MetricError(f"'{column}' is not a numeric column, so {tree.agg.upper()} cannot be used on it.")
        return Node("agg", agg=tree.agg, column=column)
    if tree.kind == "num":
        return tree
    if tree.kind == "neg":
        return Node("neg", left=resolve(tree.left, source))
    return Node("bin", op=tree.op, left=resolve(tree.left, source), right=resolve(tree.right, source))


def render(tree: Node) -> str:
    if tree.kind == "num":
        return repr(tree.value).rstrip("0").rstrip(".") if tree.value != int(tree.value) else str(int(tree.value))
    if tree.kind == "agg":
        return f'{ {"count_distinct": "COUNT_DISTINCT"}.get(tree.agg, tree.agg.upper()) }("{tree.column}")'
    if tree.kind == "neg":
        return f"-{render(tree.left)}"
    return f"({render(tree.left)} {tree.op} {render(tree.right)})"


# --------------------------------------------------------------------------
# SQL translation
# --------------------------------------------------------------------------


def to_sql(tree: Node, dialect: str, quote) -> str:
    if tree.kind == "num":
        return repr(tree.value)
    if tree.kind == "neg":
        return f"(-{to_sql(tree.left, dialect, quote)})"
    if tree.kind == "agg":
        return dialects.agg_expr(dialect, tree.agg, quote(tree.column))
    left, right = to_sql(tree.left, dialect, quote), to_sql(tree.right, dialect, quote)
    if tree.op == "/":
        return f"(({left}) * 1.0 / NULLIF(({right}), 0))"
    return f"({left} {tree.op} {right})"


# --------------------------------------------------------------------------
# pandas evaluation
# --------------------------------------------------------------------------


def _eval(tree: Node, frame: pd.DataFrame, keys: list[str]):
    if tree.kind == "num":
        return tree.value
    if tree.kind == "neg":
        return -_eval(tree.left, frame, keys)
    if tree.kind == "agg":
        series = frame[tree.column]
        if tree.agg in NUMERIC_AGGS:
            series = pd.to_numeric(series, errors="coerce")
        func = {"sum": "sum", "avg": "mean", "min": "min", "max": "max", "count": "count", "count_distinct": "nunique"}[tree.agg]
        if keys:
            return frame.assign(**{tree.column: series}).groupby(keys, dropna=False)[tree.column].agg(func)
        return getattr(series, func)()
    left, right = _eval(tree.left, frame, keys), _eval(tree.right, frame, keys)
    if tree.op == "+":
        return left + right
    if tree.op == "-":
        return left - right
    if tree.op == "*":
        return left * right
    if isinstance(right, pd.Series):
        right = right.replace(0, np.nan)
        return left / right
    return left / right if right else np.nan


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------


def alias_for(name: str) -> str:
    return slugify(name, "metric").replace("-", "_")


def execute(metric: dict, plan: QueryPlan, source: DataSource) -> ExecutionResult:
    tree = resolve(parse(metric["expression"]), source)
    alias = alias_for(metric["name"])
    if isinstance(source, ConnectedSQLSource):
        return _execute_sql(tree, alias, plan, source)
    if isinstance(source, FrameDataSource) or source.kind == "dataframe":
        return _execute_frame(tree, alias, plan, source)
    raise MetricError("Business metrics are not available for this kind of data source.")


def _execute_sql(tree: Node, alias: str, plan: QueryPlan, source: ConnectedSQLSource) -> ExecutionResult:
    from app.agents.sql_agent import _cfilter, _cquote
    from app.database.query_validator import validate_query
    from app.services import connection_service

    d = source.dialect
    quote = lambda c: _cquote(c, d)  # noqa: E731
    select, group = [], []
    if plan.time_column and plan.time_grain:
        period = dialects.period_expr(d, quote(plan.time_column), plan.time_grain)
        select.append(f"{period} AS period")
        group.append(period)
    for dim in plan.dimensions:
        select.append(quote(dim))
        group.append(quote(dim))
    select.append(f"{to_sql(tree, d, quote)} AS {alias}")
    sql = f"SELECT {dialects.top_clause(d, plan.limit)}{', '.join(select)} FROM {source.table_sql}"
    if plan.filters:
        sql += " WHERE " + " AND ".join(_cfilter(f, source) for f in plan.filters)
    if group:
        sql += " GROUP BY " + ", ".join(group)
    if plan.time_column and plan.time_grain:
        sql += " ORDER BY period ASC"
    elif plan.sort != "none" and group:
        sql += f" ORDER BY {alias} {'ASC' if plan.sort == 'asc' else 'DESC'}"
    sql = dialects.apply_limit(d, sql, plan.limit)
    safe_sql, _ = validate_query(
        sql, {source.table_name}, Config.MAX_RESULT_ROWS, dialect=d, known_columns=set(source.columns),
        allowed_schemas={source.schema} if source.schema else None,
    )
    result = connection_service.execute_for_source(source, safe_sql)
    result.operation = safe_sql
    return result


def _execute_frame(tree: Node, alias: str, plan: QueryPlan, source: DataSource) -> ExecutionResult:
    from app.agents.pandas_agent import _GRAIN_FREQ, _apply_filter

    started = time.perf_counter()
    frame = source.frame()
    for condition in plan.filters:
        frame = _apply_filter(frame, condition)
    keys = list(plan.dimensions)
    if plan.time_column and plan.time_grain:
        times = pd.to_datetime(frame[plan.time_column], errors="coerce")
        frame = frame.assign(period=times.dt.to_period(_GRAIN_FREQ.get(plan.time_grain, "MS")[0]).astype(str))
        frame = frame[times.notna()]
        keys = ["period"] + keys
    value = _eval(tree, frame, keys)
    if isinstance(value, pd.Series):
        out = value.reset_index(name=alias)
    else:
        out = pd.DataFrame([{alias: value}])
    out = out.replace([np.inf, -np.inf], np.nan)
    if plan.time_column and plan.time_grain:
        out = out.sort_values("period")
    elif plan.sort != "none" and keys:
        out = out.sort_values(alias, ascending=plan.sort == "asc")
    if plan.limit:
        out = out.head(plan.limit)
    scalar = out.iloc[0, 0] if out.shape == (1, 1) else None
    return ExecutionResult(
        success=True, columns=[str(c) for c in out.columns], rows=dataframe_to_records(out), row_count=len(out),
        scalar=json_safe(scalar), operation=f"{render(tree)}" + (f" GROUP BY {', '.join(keys)}" if keys else ""),
        engine="pandas", execution_ms=round((time.perf_counter() - started) * 1000, 2),
    )
