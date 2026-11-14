"""Dataset profiling for dashboard generation (semantic matching only against columns that really exist)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

ROLE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("profit", re.compile(r"profit|net[_ ]?income|earnings", re.I)),
    ("discount", re.compile(r"discount|rebate", re.I)),
    ("rate", re.compile(r"\b(rate|ratio|pct|percent\w*|share|score|margin)\b|_(rate|ratio|pct|percent)|(rate|ratio|pct)$", re.I)),
    ("spend", re.compile(r"marketing|advertis|\bads?\b|spend|promotion", re.I)),
    ("cost", re.compile(r"cost|expense|cogs", re.I)),
    ("price", re.compile(r"price|rate_per|per_unit|unit_", re.I)),
    ("quantity", re.compile(r"quantity|qty|units?|boxes|volume|shipped|items", re.I)),
    ("revenue", re.compile(r"revenue|sales|amount|turnover|gmv|income|total", re.I)),
]
RATE_ROLES = {"discount", "rate", "price"}
ADDITIVE_ROLES = {"revenue", "profit", "cost", "spend", "quantity"}
PRIORITY = ["revenue", "profit", "quantity", "cost", "spend", "price", "discount", "rate", "other"]
ID_NAME = re.compile(r"(^|[_ ])(id|uuid|guid|code|key|number|no)($|[_ ])|order[_ ]?id|^index$", re.I)
TARGET_NAME = re.compile(r"target|goal|quota|budget", re.I)


@dataclass
class Measure:
    name: str
    role: str
    agg: str  # "sum" | "mean"
    valid_pct: float
    nunique: int

    @property
    def label(self) -> str:
        return ("Average " if self.agg == "mean" else "") + self.name.replace("_", " ").title()


@dataclass
class Dim:
    name: str
    card: int
    filterable: bool


@dataclass
class DateInfo:
    column: str
    start: pd.Timestamp
    end: pd.Timestamp
    months: int
    days: int
    grain: str  # "month" | "day"


@dataclass
class Profile:
    rows: int
    measures: list[Measure] = field(default_factory=list)
    dims: list[Dim] = field(default_factory=list)
    date: DateInfo | None = None
    id_column: str | None = None
    hierarchies: list[tuple[str, str]] = field(default_factory=list)  # (parent, child), child strictly finer
    targets: list[str] = field(default_factory=list)
    quality: dict = field(default_factory=dict)

    def measure(self, role: str) -> Measure | None:
        return next((m for m in self.measures if m.role == role), None)

    def by_name(self, name: str) -> Measure | None:
        return next((m for m in self.measures if m.name == name), None)


def role_of(name: str, values: pd.Series) -> str:
    for role, pattern in ROLE_PATTERNS:
        if pattern.search(name):
            return role
    clean = values.dropna()
    if len(clean) and clean.min() >= 0 and clean.max() <= 1.0 and clean.nunique() > 2:
        return "rate"
    return "other"


def aggregation(role: str, values: pd.Series) -> str:
    if role in RATE_ROLES:
        return "mean"
    clean = pd.to_numeric(values, errors="coerce").dropna()
    in_unit = len(clean) > 0 and clean.min() >= 0 and clean.max() <= 1.0 and role == "other"
    return "mean" if in_unit else "sum"


def _date_info(frame: pd.DataFrame, columns: list[str]) -> DateInfo | None:
    for col in columns:
        if col not in frame.columns:
            continue
        d = pd.to_datetime(frame[col], errors="coerce").dropna()
        if len(d) < 4:
            continue
        months = d.dt.to_period("M").nunique()
        days = int((d.max() - d.min()).days) + 1
        if months >= 3 and days >= 60:
            return DateInfo(col, d.min(), d.max(), int(months), days, "month")
        if d.dt.normalize().nunique() >= 6:
            return DateInfo(col, d.min(), d.max(), int(months), days, "day")
    return None


def _hierarchies(frame: pd.DataFrame, dims: list[Dim]) -> list[tuple[str, str]]:
    out = []
    small = [d for d in dims if 2 <= d.card <= 100]
    for parent in small:
        for child in small:
            if child.name == parent.name or child.card <= parent.card:
                continue
            fan = frame.groupby(child.name, observed=True)[parent.name].nunique()
            if len(fan) and (fan <= 1).all():
                out.append((parent.name, child.name))
    return out


def build(frame: pd.DataFrame, source) -> Profile:
    rows = int(len(frame))
    prof = Profile(rows=rows)
    for name in source.measures:
        if name not in frame.columns:
            continue
        values = pd.to_numeric(frame[name], errors="coerce")
        nunique = int(values.nunique(dropna=True))
        if nunique < 2:
            continue
        if ID_NAME.search(name) and nunique > 0.9 * max(rows, 1):
            prof.id_column = prof.id_column or name
            continue
        role = role_of(name, values)
        prof.measures.append(Measure(name, role, aggregation(role, values), float(values.notna().mean() * 100), nunique))
        if TARGET_NAME.search(name):
            prof.targets.append(name)
    prof.measures.sort(key=lambda m: (PRIORITY.index(m.role) if m.role in PRIORITY else 99, -m.nunique))

    for name in source.dimensions:
        if name not in frame.columns:
            continue
        card = int(frame[name].nunique(dropna=True))
        if card < 2 or card > max(0.6 * rows, 2) and rows > 20:
            if ID_NAME.search(name) or card > 0.9 * max(rows, 1):
                prof.id_column = prof.id_column or name
            continue
        prof.dims.append(Dim(name, card, filterable=card <= 50))
    # sensible order: readable group counts first (3..30), then very small, then large
    prof.dims.sort(key=lambda d: (0 if 3 <= d.card <= 30 else 1 if d.card == 2 else 2, d.card))
    prof.date = _date_info(frame, list(source.date_columns))
    prof.hierarchies = _hierarchies(frame, prof.dims)
    prof.quality = quality(frame, prof)
    return prof


def quality(frame: pd.DataFrame, prof: Profile) -> dict:
    rows = len(frame)
    cells = frame.size or 1
    numeric_cols = [m.name for m in prof.measures]
    valid_numeric = None
    if numeric_cols and rows:
        valid_numeric = float(np.mean([pd.to_numeric(frame[c], errors="coerce").notna().mean() for c in numeric_cols]) * 100)
    q = {
        "rows": int(rows),
        "columns": int(frame.shape[1]),
        "missing_pct": round(float(frame.isna().sum().sum() / cells * 100), 2),
        "valid_numeric_pct": None if valid_numeric is None else round(valid_numeric, 2),
        "duplicate_rows": int(frame.duplicated().sum()) if rows <= 200_000 else None,
        "dimensions": len(prof.dims),
        "measures": len(prof.measures),
        "date_column": prof.date.column if prof.date else None,
        "date_start": str(prof.date.start.date()) if prof.date else None,
        "date_end": str(prof.date.end.date()) if prof.date else None,
    }
    return q
