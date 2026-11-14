"""Pandas calculations behind the dashboard (the single place numbers are produced)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.dashboard_ai.profiler import Measure, Profile
from app.utils.helpers import human_number

MAX_SCATTER = 1500


def reduce(values: pd.Series, how: str) -> float | None:
    v = pd.to_numeric(values, errors="coerce").dropna()
    if v.empty:
        return None
    return float(v.mean() if how == "mean" else v.sum())


def period_series(frame: pd.DataFrame, prof: Profile, measure: Measure) -> tuple[pd.Series, bool]:
    """Series by period (month or day). The newest period is dropped when the data stops part-way through it,
    so a half-finished month is never reported as a collapse. Returns (series, dropped_partial)."""
    if not prof.date or prof.date.column not in frame.columns:
        return pd.Series(dtype=float), False
    d = pd.to_datetime(frame[prof.date.column], errors="coerce")
    vals = pd.to_numeric(frame[measure.name], errors="coerce")
    ok = d.notna()
    d, vals = d[ok], vals[ok]
    if d.empty:
        return pd.Series(dtype=float), False
    freq = "M" if prof.date.grain == "month" else "D"
    grouped = vals.groupby(d.dt.to_period(freq))
    series = (grouped.mean() if measure.agg == "mean" else grouped.sum()).sort_index().dropna()
    dropped = False
    if freq == "M" and len(series) >= 3:
        last = series.index[-1]
        if d.max().normalize() < last.end_time.normalize() - pd.Timedelta(days=3):
            series = series.iloc[:-1]
            dropped = True
    return series, dropped


def label(p) -> str:
    return str(p)


def change(series: pd.Series) -> dict | None:
    """Latest period vs the one before it."""
    if len(series) < 2:
        return None
    cur, prev = float(series.iloc[-1]), float(series.iloc[-2])
    if prev == 0:
        return None
    pct = (cur - prev) / abs(prev) * 100
    return {"pct": round(pct, 1), "current": cur, "previous": prev, "label": f"{series.index[-1]} vs {series.index[-2]}",
            "direction": "up" if pct > 0.05 else "down" if pct < -0.05 else "flat"}


def by_dim(frame: pd.DataFrame, dim: str, measure: Measure) -> pd.DataFrame:
    g = frame.groupby(frame[dim].astype(str), observed=True)
    vals = pd.to_numeric(frame[measure.name], errors="coerce")
    agg = vals.groupby(frame[dim].astype(str)).mean() if measure.agg == "mean" else vals.groupby(frame[dim].astype(str)).sum()
    out = pd.DataFrame({"label": agg.index, "value": agg.values, "records": g.size().reindex(agg.index).values})
    out = out.dropna(subset=["value"]).sort_values("value", ascending=False, kind="stable").reset_index(drop=True)
    total = out["value"].sum()
    out["share"] = (out["value"] / total * 100) if (measure.agg == "sum" and total) else np.nan
    return out


def unusual_periods(series: pd.Series) -> list[dict]:
    """Periods far outside the typical range of the others (robust z-score >= 3.5). Needs 8+ periods; otherwise nothing is claimed."""
    if len(series) < 8:
        return []
    x = series.astype(float).values
    med = np.median(x)
    mad = np.median(np.abs(x - med))
    if mad == 0:
        return []
    z = 0.6745 * (x - med) / mad
    out = []
    for i in np.where(np.abs(z) >= 3.5)[0]:
        out.append({"label": str(series.index[i]), "value": float(x[i]), "z": round(float(z[i]), 1),
                    "direction": "high" if z[i] > 0 else "low", "typical": float(med)})
    return out


def pearson(frame: pd.DataFrame, a: str, b: str) -> dict | None:
    x = pd.to_numeric(frame[a], errors="coerce")
    y = pd.to_numeric(frame[b], errors="coerce")
    ok = x.notna() & y.notna()
    n = int(ok.sum())
    if n < 30 or x[ok].nunique() < 3 or y[ok].nunique() < 3:
        return None
    r = float(np.corrcoef(x[ok], y[ok])[0, 1])
    if not np.isfinite(r):
        return None
    strength = "strong" if abs(r) >= 0.6 else "moderate" if abs(r) >= 0.3 else "weak"
    return {"a": a, "b": b, "r": r, "n": n, "strength": strength}


def sample_points(frame: pd.DataFrame, a: str, b: str):
    x = pd.to_numeric(frame[a], errors="coerce")
    y = pd.to_numeric(frame[b], errors="coerce")
    ok = x.notna() & y.notna()
    pts = pd.DataFrame({"x": x[ok], "y": y[ok]})
    sampled = len(pts) > MAX_SCATTER
    if sampled:
        pts = pts.sample(MAX_SCATTER, random_state=7).sort_index()
    return pts["x"].tolist(), pts["y"].tolist(), sampled


def histogram(values: pd.Series, bins: int = 12):
    v = pd.to_numeric(values, errors="coerce").dropna()
    if len(v) < 30 or v.nunique() < 5:
        return None
    counts, edges = np.histogram(v, bins=min(bins, max(5, int(np.sqrt(len(v))))))
    return edges.tolist(), counts.tolist(), float(v.median()), float(v.quantile(.25)), float(v.quantile(.75)), float(v.min()), float(v.max())


def box_stats(frame: pd.DataFrame, dim: str, measure: str, min_n: int = 5):
    vals = pd.to_numeric(frame[measure], errors="coerce")
    groups, stats = [], []
    for g, idx in frame.groupby(frame[dim].astype(str), observed=True).groups.items():
        v = vals.loc[idx].dropna()
        if len(v) < min_n:
            continue
        q1, med, q3 = (float(v.quantile(q)) for q in (.25, .5, .75))
        iqr = q3 - q1
        lo = float(v[v >= q1 - 1.5 * iqr].min())
        hi = float(v[v <= q3 + 1.5 * iqr].max())
        groups.append(g)
        stats.append({"q1": q1, "median": med, "q3": q3, "lo": lo, "hi": hi, "mean": float(v.mean()), "n": int(len(v))})
    return groups, stats


def topn_variants(df: pd.DataFrame, cap: int = 100) -> dict:
    """The same validated ranking cut four ways for the Top 5 / Top 10 / Bottom 5 / View all switch."""
    return {"top5": df.head(5), "top10": df.head(10), "bottom5": df.tail(5).iloc[::-1] if len(df) > 5 else df, "all": df.head(cap)}


def fmt(v) -> str:
    return human_number(v)


def readable(name: str) -> str:
    """Column name as a person would say it (Order_Date -> Order Date)."""
    return str(name).replace("_", " ").strip().title() if str(name).islower() or "_" in str(name) else str(name)
