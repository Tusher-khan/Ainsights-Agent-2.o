"""Fact / interpretation / recommendation for an ordinary result, built only from validated rows.

Every number quoted here is read from the executed result (never typed by a model, never estimated).
Interpretation says what the figure appears to mean *inside this dataset*; the recommendation is
phrased as something to examine next, not as a proven action. Neither claims a cause.
"""

from __future__ import annotations

from app.core.datasource import DataSource
from app.core.models import ExecutionResult, QueryPlan
from app.utils.helpers import human_number


def _readable(name: str) -> str:
    return str(name).replace("_", " ").strip()


def _numeric_rows(result: ExecutionResult, column: str) -> list[dict]:
    return [r for r in result.rows if isinstance(r.get(column), (int, float)) and not isinstance(r.get(column), bool)]


def next_analyses(result: ExecutionResult, plan: QueryPlan, source: DataSource) -> list[str]:
    """Follow-up questions made only from this dataset's real columns and this result's real values."""
    if result.row_count == 0:
        return []
    measure = plan.measures[0] if plan.measures else None
    out: list[str] = []
    dims = plan.dimensions
    if dims and measure and result.row_count >= 2 and len(result.columns) >= 2 and not plan.time_grain:
        label_col, value_col = result.columns[0], result.columns[-1]
        rows = _numeric_rows(result, value_col)
        if rows:
            ordered = sorted(rows, key=lambda r: r[value_col], reverse=True)
            lead, second, last = ordered[0], (ordered[1] if len(ordered) > 1 else None), ordered[-1]
            others = [d for d in source.dimensions if d.lower() != dims[0].lower()]
            if others:
                out.append(f"Show {measure} by {others[0]} where {dims[0]} is {lead[label_col]}")
            if second is not None:
                out.append(f"Compare {lead[label_col]} with {second[label_col]}")
            if last[label_col] != lead[label_col]:
                out.append(f"Why is {last[label_col]} lower?")
    elif not dims and measure and not plan.time_grain:
        if source.dimensions:
            out.append(f"Which {source.dimensions[0]} has the highest {measure}?")
        if source.dimensions[1:]:
            out.append(f"Show {measure} by {source.dimensions[1]}")
    if source.date_columns and not plan.time_grain and measure:
        out.append(f"Show monthly {measure} trend")
    if plan.time_grain and measure and source.dimensions:
        out.append(f"Which {source.dimensions[0]} contributes most to {measure}?")
    other_measures = [m for m in source.measures if m not in plan.measures]
    if other_measures:
        out.append(f"Do the same for {other_measures[0]}")
    seen, unique = set(), []
    for q in out:
        if q.lower() not in seen:
            seen.add(q.lower()); unique.append(q)
    return unique[:4]


def build(result: ExecutionResult, plan: QueryPlan, source: DataSource) -> dict | None:
    if result is None or result.row_count == 0 or not plan:
        return None
    measure = plan.measures[0] if plan.measures else "records"
    measure_name = _readable(measure)
    scope = ""
    if plan.filters:
        scope = " for " + ", ".join(f"{f.column} = {f.value}" for f in plan.filters if f.operator == "=") if any(f.operator == "=" for f in plan.filters) else ""
    fact = interpretation = recommendation = None

    if result.row_count == 1 and len(result.columns) == 1:
        value = result.rows[0][result.columns[0]]
        if isinstance(value, (int, float)):
            records = f" across {source.row_count:,} records" if not plan.filters else ""
            fact = f"The {_readable(result.columns[0]).lower()}{scope} is {human_number(value)}{records}."
            interpretation = (f"This is one overall figure for the selected data. It becomes more useful "
                              f"when you see how it divides across {source.dimensions[0]} or changes over time." if source.dimensions else
                              "This is one overall figure for the selected data.")
            recommendation = ("Break it down before acting on it: find which segments contribute most, then check whether the pattern holds over time."
                              if source.dimensions else None)

    elif plan.time_grain and plan.time_column and len(result.columns) >= 2 and result.row_count >= 3:
        period, value_col = result.columns[0], result.columns[-1]
        pts = _numeric_rows(result, value_col)
        if len(pts) >= 3:
            peak = max(pts, key=lambda r: r[value_col]); low = min(pts, key=lambda r: r[value_col])
            fact = (f"{measure_name.title()}{scope} peaked in {peak[period]} at {human_number(peak[value_col])} "
                    f"and was lowest in {low[period]} at {human_number(low[value_col])} across {len(pts)} {plan.time_grain}s.")
            interpretation = "The series shows how the measure moved period to period. A single high or low period is a prompt to look closer, not a trend on its own."
            recommendation = f"Investigate {low[period]} first (and {peak[period]} for what went right) by breaking those periods down by another dimension."

    elif len(result.columns) >= 2 and result.row_count >= 2 and plan.dimensions:
        label_col, value_col = result.columns[0], result.columns[-1]
        rows = _numeric_rows(result, value_col)
        if len(rows) >= 2:
            ordered = sorted(rows, key=lambda r: r[value_col], reverse=True)
            high, low = ordered[0], ordered[-1]
            summable = plan.aggregation in {"sum", "count"}
            total = sum(r[value_col] for r in rows)
            ascending = getattr(plan, "sort", "desc") == "asc"
            focus, other = (low, high) if ascending else (high, low)  # the one the user asked about
            share = f", {focus[value_col] / total * 100:.1f}% of the {len(rows)} groups shown" if summable and total else ""
            dim = _readable(plan.dimensions[0]).lower()
            if ascending:
                fact = (f"{focus[label_col]} is the lowest {measure_name.lower()}{scope} at {human_number(focus[value_col])}{share}; "
                        f"{other[label_col]} is the highest at {human_number(other[value_col])}.")
                interpretation = (f"{focus[label_col]} is the weakest {dim} by {measure_name.lower()} in this dataset. "
                                  "This describes the data; it does not by itself explain the difference.")
            else:
                fact = (f"{focus[label_col]} has the highest {measure_name.lower()}{scope} at {human_number(focus[value_col])}{share}; "
                        f"{other[label_col]} has the lowest at {human_number(other[value_col])}.")
                interpretation = (f"{focus[label_col]} is the strongest {dim} by {measure_name.lower()} in this dataset"
                                  + (" and the gap to the lowest is large." if other[value_col] and focus[value_col] / max(other[value_col], 1e-9) > 2 else ".")
                                  + " This describes the data; it does not by itself explain the difference.")
            others = [d for d in source.dimensions if d.lower() != plan.dimensions[0].lower()]
            where = f" ({', '.join(others[:2])})" if others else ""
            recommendation = (f"Before reallocating anything, examine what makes up {focus[label_col]}'s figure{where} and compare it with {other[label_col]}.")

    if not fact:
        return None
    return {
        "fact": fact,
        "interpretation": interpretation,
        "recommendation": recommendation,
        "next": next_analyses(result, plan, source),
    }
