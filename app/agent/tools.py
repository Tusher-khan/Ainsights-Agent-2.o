"""Tool registry for the business analysis agent.

Every capability the agent (or a desktop / mobile / API client) can invoke is a named tool
that declares: description, input schema, output keys, required permissions, data-source
requirements, risk level and timeout. `call()` is the single gate:

    signed-in user → tool exists → role has its permissions → arguments match the schema
    → the data source belongs to the user and meets the tool's requirements
    → runs (with a timeout) inside the owner scope → audit event

Ids supplied by a client or by a language model are only ever keys into *that user's own*
rows, so another user's source or dataset is simply "not found".

Audit events record the tool, source id, success, duration, error class, model and retry
count. They never store arguments, SQL text, credentials or result data.
"""

from __future__ import annotations

import contextvars
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable

from config import Config
from app.core.datasource import ConnectedSQLSource, DataSource, from_dataset_record
from app.core.models import QueryPlan
from app.database import repository
from app.database.connection import app_store
from app.security import permissions as perm
from app.security.request_scope import owner_scope
from app.utils.exceptions import AInsightError, DatasetNotFoundError, UnsupportedRequestError
from app.utils.helpers import json_safe, new_id, utcnow
from app.utils.logger import get_logger

log = get_logger("agent.tools")

_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="agent-tool")


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    func: Callable[..., Any]
    output_keys: tuple = ()
    permissions: tuple = (perm.ANALYZE,)
    requires: tuple = ()  # dataset | date_column | measure | connected_sql | rest_source
    risk: str = "low"  # low | medium | high
    timeout: int = 60
    produces_numbers: bool = True

    @property
    def parameters(self) -> dict:  # kept for the earlier API shape
        return self.input_schema

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema,
                "output_schema": {"keys": list(self.output_keys)}, "permissions": list(self.permissions),
                "data_source_requirements": list(self.requires), "risk_level": self.risk, "timeout_seconds": self.timeout}


# --------------------------------------------------------------------------
# argument validation (a small JSON-schema subset, enough to reject bad calls safely)
# --------------------------------------------------------------------------

_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list}


def validate_args(schema: dict, args: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(args, dict):
        return ["arguments must be an object"]
    props = schema.get("properties", {})
    for key in args:
        if key not in props:
            problems.append(f"unknown argument '{key}'")
    for key in schema.get("required", []):
        if key not in args:
            problems.append(f"missing argument '{key}'")
    for key, value in args.items():
        spec = props.get(key)
        if not spec:
            continue
        expected = _TYPES.get(spec.get("type", ""))
        if expected and (not isinstance(value, expected) or (spec["type"] in {"integer", "number"} and isinstance(value, bool))):
            problems.append(f"'{key}' must be {spec['type']}")
            continue
        if spec.get("enum") and value not in spec["enum"]:
            problems.append(f"'{key}' must be one of {spec['enum']}")
        if isinstance(value, str) and len(value) > spec.get("maxLength", 4000):
            problems.append(f"'{key}' is too long")
        if isinstance(value, list) and len(value) > spec.get("maxItems", 5000):
            problems.append(f"'{key}' has too many items")
    return problems


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _source(dataset_id: str) -> DataSource:
    record = repository.get_dataset(str(dataset_id))  # owner-scoped lookup
    if not record:
        raise DatasetNotFoundError()
    return from_dataset_record(record)


def trend_series(s: DataSource, measure: str, grain: str):
    """(measure, period labels, values), aggregated inside the source itself (SQL or pandas)."""
    from app.agents import pandas_agent, sql_agent

    column = s.resolve_column(measure)
    if not column or column not in s.measures:
        raise UnsupportedRequestError("Choose a numeric measure from this dataset.")
    if not s.date_columns:
        raise UnsupportedRequestError("This dataset has no date column, so no trend can be computed.")
    plan = QueryPlan(intent="trend_analysis", dataset=s.name, dimensions=[], measures=[column], aggregation="sum",
                     sort="none", time_column=s.date_columns[0], time_grain=grain)
    if s.kind == "sql":
        result, _r, _i = sql_agent.run("(tool) trend", plan, s)
    else:
        result, _o = pandas_agent.execute(plan, s)
    labels = [str(r[result.columns[0]]) for r in result.rows]
    values = [float(r[result.columns[-1]]) for r in result.rows]
    return column, labels, values


def _check_requirements(tool: Tool, args: dict) -> None:
    ids = [args[k] for k in ("dataset_id", "dataset_a", "dataset_b") if k in args]
    for dataset_id in ids:
        s = _source(dataset_id)
        if "date_column" in tool.requires and not s.date_columns:
            raise UnsupportedRequestError(f"“{s.name}” has no date column, which this analysis needs.")
        if "measure" in tool.requires and not s.measures:
            raise UnsupportedRequestError(f"“{s.name}” has no numeric column, which this analysis needs.")
        if "connected_sql" in tool.requires and not isinstance(s, ConnectedSQLSource):
            raise UnsupportedRequestError("This tool only works on tables from a connected database.")


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------


def get_data_sources() -> dict:
    from app.services import connection_service, dataset_service

    return {"connections": connection_service.list_sources(), "datasets": dataset_service.list_datasets()}


def get_dataset_metadata(dataset_id: str) -> dict:
    s = _source(dataset_id)
    return {
        "id": s.id, "name": s.name, "kind": s.kind, "row_count": s.row_count,
        "columns": [{"name": c["name"], "type": c["semantic_type"], "dtype": c["dtype"],
                     "missing_pct": c["null_percentage"], "distinct": c["unique_count"]} for c in s.profile.get("columns", [])],
        "measures": s.measures, "dimensions": s.dimensions, "date_columns": s.date_columns,
        "primary_keys": s.profile.get("primary_keys", []), "foreign_keys": s.profile.get("foreign_keys", []),
    }


def get_schema(source_id: str) -> dict:
    from app.services import connection_service

    return connection_service.discover(str(source_id))


def plan_analysis(dataset_id: str, question: str) -> dict:
    """Turn a question into a structured, validated analysis plan (LLM when available, rules otherwise)."""
    from app.ai import query_planner
    from app.ai.intent_classifier import classify_with_rules

    s = _source(dataset_id)
    intent = classify_with_rules(question).intent
    plan, method = query_planner.plan_query(question, s, intent)
    return {"plan": plan.model_dump(), "method": method, "intent": intent}


def query_database(dataset_id: str, sql: str) -> dict:
    """Run one read-only SELECT on a connected database table. Validated before it runs."""
    from app.database.query_validator import validate_query
    from app.services import connection_service

    s = _source(dataset_id)
    if not isinstance(s, ConnectedSQLSource):
        raise UnsupportedRequestError("query_database only works on tables from a connected database.")
    safe, report = validate_query(
        sql, {s.table_name}, Config.MAX_RESULT_ROWS, dialect=s.dialect, known_columns=set(s.columns),
        allowed_schemas={s.schema} if s.schema else None,
    )
    result = connection_service.execute_for_source(s, safe)
    return {"sql": safe, "columns": result.columns, "rows": result.rows, "row_count": result.row_count,
            "truncated": result.truncated, "checks": report.model_dump()}


def analyze_dataframe(dataset_id: str, plan: dict) -> dict:
    from app.services import analytics_service

    return analytics_service.run_plan(plan, str(dataset_id))


def call_business_api(source_id: str, rows: int = 20) -> dict:
    """Preview records from a connected REST API. Credentials stay inside the connector."""
    from app.services import connection_service

    record = connection_service.get_source(str(source_id))
    if record["kind"] != "rest":
        raise UnsupportedRequestError("call_business_api only works on REST API connections.")
    return connection_service.preview(str(source_id), "", rows)


def calculate_metric(dataset_id: str, metric: str, dimensions: list | None = None, time_grain: str | None = None,
                     time_column: str | None = None) -> dict:
    from app.agent import metrics as engine
    from app.database import metric_repo

    s = _source(dataset_id)
    found = next((m for m in metric_repo.list_for(s.id) if m["name"].lower() == str(metric).lower()), None)
    if not found:
        raise UnsupportedRequestError(f"No metric named '{metric}' is defined for this dataset.")
    dims = [s.resolve_column(d) for d in (dimensions or [])]
    if any(d is None for d in dims):
        raise UnsupportedRequestError("One of those dimensions is not a column of this dataset.")
    tcol = s.resolve_column(time_column) if time_column else (s.date_columns[0] if time_grain and s.date_columns else None)
    plan = QueryPlan(intent="grouping", dataset=s.name, dimensions=dims, measures=[], aggregation="sum", sort="none",
                     time_column=tcol if time_grain else None, time_grain=time_grain or None)
    result = engine.execute(found, plan, s)
    return {"metric": found["name"], "definition": found["expression"], "columns": result.columns,
            "rows": result.rows, "engine": result.engine, "sql": result.sql}


def compare_periods(dataset_id: str, question: str) -> dict:
    from app.ai import investigation_engine

    return investigation_engine.investigate(question, _source(dataset_id)).to_dict()


def period_contribution(dataset_id: str, measure: str, dimension: str | None = None, filters: dict | None = None,
                        grain: str = "month", current_period: str | None = None) -> dict:
    from app.agent import analysis_tools

    return analysis_tools.period_contribution(_source(dataset_id), measure, dimension, filters, grain, current_period)


def detect_anomalies(dataset_id: str, measure: str, grain: str = "month") -> dict:
    import pandas as pd

    from app.ai import investigation_engine

    s = _source(dataset_id)
    column, labels, vals = trend_series(s, measure, grain)
    found = investigation_engine.detect_anomalies(pd.Series(vals, dtype=float), labels)
    return {
        "measure": column, "grain": grain, "periods_checked": len(labels), "anomalies": found,
        "series": [{"period": l, "value": v} for l, v in zip(labels, vals)],
        "alerts": [f"{column.replace('_', ' ').title()} {a['direction']}d {abs(a['change_pct']):.1f}% in {a['period']}, "
                   f"well outside its recent pattern." for a in found],
        "checks": [{"name": "enough_periods", "passed": len(labels) >= 6, "detail": f"{len(labels)} periods"}],
        "limitations": [] if len(labels) >= 6 else ["Fewer than 6 periods: anomaly detection is unreliable."],
        "note": "A statistical flag (robust z-score), not proof of a problem.",
    }


def correlate(dataset_id: str, x: str, y: str) -> dict:
    from app.agent import analysis_tools

    return analysis_tools.correlate(_source(dataset_id), x, y)


def describe_distribution(dataset_id: str, measure: str, bins: int = 12) -> dict:
    from app.agent import analysis_tools

    return analysis_tools.describe_distribution(_source(dataset_id), measure, bins)


def data_quality(dataset_id: str) -> dict:
    from app.agent import analysis_tools

    return analysis_tools.data_quality(_source(dataset_id))


def compare_trends(dataset_a: str, measure_a: str, dataset_b: str, measure_b: str, grain: str = "month") -> dict:
    from app.agent import analysis_tools

    return analysis_tools.compare_trends(_source(dataset_a), measure_a, _source(dataset_b), measure_b, grain)


def generate_visualization(plan: dict, columns: list, rows: list) -> dict:
    from app.agents.visualization_agent import build_figure
    from app.core.models import ExecutionResult

    result = ExecutionResult(success=True, columns=list(columns), rows=list(rows), row_count=len(rows), engine="client")
    figure = build_figure(QueryPlan.model_validate(plan), result)
    return figure or {"figure": None, "reason": "A table is the honest choice for this result shape."}


def validate_result(dataset_id: str, plan: dict, columns: list, rows: list) -> dict:
    from app.agents.validation_agent import validate_result as validate
    from app.core.models import ExecutionResult

    result = ExecutionResult(success=True, columns=list(columns), rows=list(rows), row_count=len(rows), engine="client")
    return validate(result, QueryPlan.model_validate(plan), _source(dataset_id)).model_dump()


def compare_sources(question: str) -> dict:
    from app.agent import multi_source

    return multi_source.run(question)


def _obj(props: dict, required: list | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


_S = {"type": "string"}
_ID = {"type": "string", "maxLength": 80}
_GRAIN = {"type": "string", "enum": ["week", "month", "quarter", "year"]}

TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("get_data_sources", "List the user's connected data sources and datasets.", _obj({}), get_data_sources,
         ("connections", "datasets"), (perm.READ, perm.SOURCES), (), "low", 20, False),
    Tool("get_dataset_metadata", "Columns, types, measures, dimensions, date columns and keys of one dataset.",
         _obj({"dataset_id": _ID}, ["dataset_id"]), get_dataset_metadata, ("columns", "measures", "dimensions"),
         (perm.READ,), ("dataset",), "low", 20, False),
    Tool("get_schema", "Tables available in a connected data source.", _obj({"source_id": _ID}, ["source_id"]), get_schema,
         ("tables",), (perm.SOURCES,), (), "medium", 60, False),
    Tool("plan_analysis", "Build a structured analysis plan (measures, dimensions, filters, time grain) from a question.",
         _obj({"dataset_id": _ID, "question": {"type": "string", "maxLength": 1000}}, ["dataset_id", "question"]),
         plan_analysis, ("plan",), (perm.ANALYZE,), ("dataset",), "low", 30, False),
    Tool("query_database", "Run one read-only SELECT on a connected database table (validated before it runs).",
         _obj({"dataset_id": _ID, "sql": {"type": "string", "maxLength": 4000}}, ["dataset_id", "sql"]), query_database,
         ("rows", "columns"), (perm.SQL,), ("dataset", "connected_sql"), "high", 45),
    Tool("analyze_dataframe", "Run a structured analysis plan on any dataset (SQL or pandas, chosen automatically).",
         _obj({"dataset_id": _ID, "plan": {"type": "object"}}, ["dataset_id", "plan"]), analyze_dataframe,
         ("result", "validation"), (perm.ANALYZE,), ("dataset",), "medium", 60),
    Tool("call_business_api", "Preview records from a connected REST API (credentials are never exposed).",
         _obj({"source_id": _ID, "rows": {"type": "integer"}}, ["source_id"]), call_business_api, ("columns", "rows"),
         (perm.REST,), (), "high", 40),
    Tool("calculate_metric", "Evaluate a business metric defined in the semantic layer.",
         _obj({"dataset_id": _ID, "metric": {"type": "string", "maxLength": 120}, "dimensions": {"type": "array", "maxItems": 4},
               "time_grain": _GRAIN, "time_column": _S}, ["dataset_id", "metric"]),
         calculate_metric, ("rows", "definition"), (perm.METRICS,), ("dataset",), "low", 60),
    Tool("compare_periods", "Investigate why a measure changed between the latest two periods (drivers, hotspots, anomalies).",
         _obj({"dataset_id": _ID, "question": {"type": "string", "maxLength": 1000}}, ["dataset_id", "question"]),
         compare_periods, ("kpis", "drivers"), (perm.ANALYZE,), ("dataset", "date_column", "measure"), "medium", 60),
    Tool("period_contribution",
         "How a measure changed between two periods, optionally inside a slice (filters {column: value}) and split by one "
         "dimension. Use it to drill into a segment found earlier.",
         _obj({"dataset_id": _ID, "measure": _S, "dimension": _S, "filters": {"type": "object"}, "grain": _GRAIN,
               "current_period": {"type": "string", "maxLength": 20}}, ["dataset_id", "measure"]),
         period_contribution, ("total", "rows"), (perm.ANALYZE,), ("dataset", "date_column", "measure"), "medium", 60),
    Tool("detect_anomalies", "Flag unusual period-over-period moves in a measure.",
         _obj({"dataset_id": _ID, "measure": _S, "grain": _GRAIN}, ["dataset_id", "measure"]), detect_anomalies,
         ("anomalies", "series"), (perm.ANALYZE,), ("dataset", "date_column", "measure"), "low", 60),
    Tool("correlate", "Measure the relationship (Pearson/Spearman) between two numeric columns.",
         _obj({"dataset_id": _ID, "x": _S, "y": _S}, ["dataset_id", "x", "y"]), correlate, ("pearson", "points"),
         (perm.ANALYZE,), ("dataset", "measure"), "low", 60),
    Tool("describe_distribution", "Histogram and summary statistics (median, quartiles, outliers) of a numeric column.",
         _obj({"dataset_id": _ID, "measure": _S, "bins": {"type": "integer"}}, ["dataset_id", "measure"]),
         describe_distribution, ("histogram",), (perm.ANALYZE,), ("dataset", "measure"), "low", 60),
    Tool("data_quality", "Scan a dataset for missing values, duplicates, extreme values and unparseable dates.",
         _obj({"dataset_id": _ID}, ["dataset_id"]), data_quality, ("issues",), (perm.ANALYZE,), ("dataset",), "low", 60),
    Tool("compare_trends", "Relate a measure in one source to a measure in another source over time (each aggregated in its own source).",
         _obj({"dataset_a": _ID, "measure_a": _S, "dataset_b": _ID, "measure_b": _S,
               "grain": {"type": "string", "enum": ["month", "year"]}}, ["dataset_a", "measure_a", "dataset_b", "measure_b"]),
         compare_trends, ("a", "b", "periods"), (perm.ANALYZE,), ("dataset", "date_column", "measure"), "medium", 90),
    Tool("generate_visualization", "Choose and build the right chart for an executed result.",
         _obj({"plan": {"type": "object"}, "columns": {"type": "array"}, "rows": {"type": "array", "maxItems": 5000}},
              ["plan", "columns", "rows"]), generate_visualization, ("figure",), (perm.ANALYZE,), (), "low", 20, False),
    Tool("validate_result", "Run the result validation checks on an executed result.",
         _obj({"dataset_id": _ID, "plan": {"type": "object"}, "columns": {"type": "array"},
               "rows": {"type": "array", "maxItems": 5000}}, ["dataset_id", "plan", "columns", "rows"]),
         validate_result, ("valid", "checks"), (perm.ANALYZE,), ("dataset",), "low", 20, False),
    Tool("compare_sources", "Compare actual data in one source with a forecast/target/budget in another.",
         _obj({"question": {"type": "string", "maxLength": 1000}}, ["question"]), compare_sources, ("result",),
         (perm.ANALYZE,), (), "medium", 90),
]}


def tool_specs(names: set | None = None) -> list[dict]:
    """Machine-readable tool list for an LLM tool-calling loop or a client SDK."""
    return [t.spec() for t in TOOLS.values() if names is None or t.name in names]


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------


def _record(analysis_id, tool, source_id, ok, ms, error, model=None, retries=0) -> None:
    try:
        with app_store() as conn:
            conn.execute(
                "INSERT INTO agent_events (id,owner_id,analysis_id,tool,source_id,ok,ms,error,model,retries,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("evt"), owner_scope(), analysis_id, tool, source_id, 1 if ok else 0, round(ms, 2), error, model,
                 retries, utcnow()),
            )
    except Exception as exc:  # observability must never break an analysis
        log.warning("Could not record agent event: %s", exc)


class ToolDenied(AInsightError):
    status_code = 403
    user_message = "That analysis step is not allowed."


def validate_call(name: str, args: dict | None, *, allowed: set | None = None, role: str | None = None) -> Tool:
    """Everything that can be checked before running: raises a safe error for an invalid call."""
    owner_scope()  # fail closed: raises when accounts are on and nobody is signed in
    tool = TOOLS.get(name)
    if tool is None:
        raise UnsupportedRequestError("That tool does not exist.", f"unknown tool {name!r}")
    if allowed is not None and name not in allowed:
        raise ToolDenied("That tool is not appropriate for this analysis.", f"{name} not in the allowed set")
    missing_permissions = set(tool.permissions) - perm.permissions_for(role)
    if missing_permissions:
        raise ToolDenied("Your role does not have permission to use that tool.", f"missing {sorted(missing_permissions)}")
    problems = validate_args(tool.input_schema, args or {})
    if problems:
        raise UnsupportedRequestError("The tool call was not valid.", "; ".join(problems))
    return tool


def call(name: str, args: dict | None = None, analysis_id: str | None = None, *, allowed: set | None = None,
         role: str | None = None, retries: int | None = None, model: str | None = None) -> Any:
    """Authorize, validate, execute (with a timeout and bounded retries) and audit one tool call."""
    tool = validate_call(name, args, allowed=allowed, role=role)
    args = args or {}
    source_id = str(args.get("dataset_id") or args.get("source_id") or args.get("dataset_a") or "") or None
    max_retries = Config.AGENT_MAX_TOOL_RETRIES if retries is None else retries
    started = time.perf_counter()
    attempt = 0
    while True:
        try:
            _check_requirements(tool, args)
            ctx = contextvars.copy_context()
            future = _POOL.submit(ctx.run, tool.func, **args)
            try:
                out = future.result(timeout=tool.timeout)
            except FutureTimeout as exc:
                future.cancel()
                raise AInsightError("That analysis step took too long and was stopped.", f"{name} exceeded {tool.timeout}s") from exc
            break
        except AInsightError as exc:
            # Only transient connector/timeouts are worth another attempt; bad input or denials are not.
            transient = type(exc).__name__ in {"ConnectorError"} or "took too long" in exc.user_message
            if transient and attempt < max_retries:
                attempt += 1
                continue
            _record(analysis_id, name, source_id, False, (time.perf_counter() - started) * 1000, type(exc).__name__, model, attempt)
            raise
        except Exception as exc:
            _record(analysis_id, name, source_id, False, (time.perf_counter() - started) * 1000, type(exc).__name__, model, attempt)
            log.exception("Tool %s crashed", name)
            raise AInsightError("That analysis step failed unexpectedly.", f"{type(exc).__name__}") from exc
    _record(analysis_id, name, source_id, True, (time.perf_counter() - started) * 1000, None, model, attempt)
    return json_safe(out)


def recent_events(limit: int = 100) -> list[dict]:
    owner = owner_scope()
    with app_store() as conn:
        if owner is None:
            rows = conn.execute("SELECT * FROM agent_events ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM agent_events WHERE owner_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                                (owner, limit)).fetchall()
    return [dict(r) for r in rows]
