"""Investigation engine: finds real drivers, refuses to over-claim, reconciles.

The data here is synthetic with a *known* answer, so each test can assert the
engine found what was planted — and, just as important, that it does not
report noise as a finding.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.ai import investigation_engine as ie
from app.ai.intent_classifier import classify_with_rules
from app.core.datasource import FrameDataSource
from app.files.file_profiler import build_profile
from app.utils.exceptions import UnsupportedRequestError


def _source(frame: pd.DataFrame, name: str = "synthetic") -> FrameDataSource:
    profile = build_profile(frame.copy(), name, "csv")

    class InMemory(FrameDataSource):
        def frame(self):
            return frame.copy()

    return InMemory(id=f"ds_{name}", name=name, kind="dataframe", profile=profile)


def _orders(months: int = 12, collapse: bool = False, partial_last: bool = False) -> pd.DataFrame:
    """Steady business: every (region, category) cell sells ~100 per order, 5 orders a month.

    With collapse=True, South x Furniture falls to 5% in the final month — an
    effect far outside the (small) normal variation.
    """
    rng = np.random.default_rng(7)
    rows = []
    periods = pd.period_range("2024-01", periods=months, freq="M")
    for period in periods:
        last = period == periods[-1]
        for region in ["North", "South", "East", "West"]:
            for category in ["Furniture", "Technology", "Office"]:
                for day in (1, 8, 15, 22, period.days_in_month):  # reaches month-end: a complete month
                    sales = 100 + rng.normal(0, 4)
                    if collapse and last and region == "South" and category == "Furniture":
                        sales *= 0.05
                    rows.append((period.start_time + pd.Timedelta(days=day - 1), region, category, round(sales, 2)))
    frame = pd.DataFrame(rows, columns=["Order Date", "Region", "Category", "Sales"])
    frame["Profit"] = (frame["Sales"] * 0.2).round(2)
    if partial_last:
        frame = frame[frame["Order Date"] <= frame["Order Date"].max().replace(day=8)]
    return frame


# --- finding the planted driver ---------------------------------------------


def test_finds_the_planted_combination_and_rates_it_unusual():
    inv = ie.investigate("Why did our sales decrease last month?", _source(_orders(collapse=True)))

    assert inv.measure == "Sales"
    assert inv.kpis[0]["direction"] == "down"
    top = inv.hotspots[0]
    assert set(top["segments"]) == {"South", "Furniture"}
    assert top["unusual"] is True  # far outside its normal month-to-month swing
    # the sharpest hotspot explains essentially all of the change
    assert top["share_of_change_pct"] > 90
    assert "South" in inv.summary and "Furniture" in inv.summary


def test_segment_changes_reconcile_with_the_total():
    inv = ie.investigate("why did sales fall", _source(_orders(collapse=True)))
    reconcile = [c for c in inv.checks if c["name"].startswith("reconciles:")]
    assert reconcile and all(c["passed"] for c in reconcile)


def test_every_reported_number_is_traceable_to_the_data():
    frame = _orders(collapse=True)
    inv = ie.investigate("why did sales fall", _source(frame))
    frame["p"] = frame["Order Date"].dt.to_period("M")
    expected_current = round(float(frame[frame.p == pd.Period("2024-12")]["Sales"].sum()), 4)
    assert inv.kpis[0]["current"] == pytest.approx(expected_current, abs=1e-3)


# --- not over-claiming --------------------------------------------------------


def test_stable_business_produces_no_unusual_findings():
    inv = ie.investigate("why did sales change", _source(_orders(collapse=False)))
    assert inv.anomalies == []
    assert not any(h.get("unusual") is True for h in inv.hotspots)


def test_interpretation_never_claims_a_cause():
    inv = ie.investigate("why did sales fall", _source(_orders(collapse=True)))
    text = " ".join(inv.interpretation).lower()
    assert "not confirmed causes" in text


def test_incomplete_last_period_is_not_compared_as_if_complete():
    inv = ie.investigate("why did sales fall", _source(_orders(partial_last=True)))
    assert inv.current["label"] == "Nov 2024"  # December ends on the 8th
    assert any("incomplete" in a for a in inv.assumptions)


def test_no_date_column_is_reported_not_guessed():
    frame = _orders().drop(columns=["Order Date"])
    with pytest.raises(UnsupportedRequestError) as exc:
        ie.investigate("why did sales fall", _source(frame))
    assert "date column" in exc.value.user_message


def test_single_period_cannot_be_compared():
    frame = _orders(months=1)
    with pytest.raises(UnsupportedRequestError):
        ie.investigate("why did sales fall", _source(frame))


# --- anomaly detection ----------------------------------------------------------


def test_anomaly_detector_flags_a_spike_and_ignores_flat_series():
    labels = [f"P{i}" for i in range(12)]
    spike = pd.Series([100, 102, 99, 101, 100, 103, 100, 250, 101, 99, 100, 102], dtype=float)
    flat = pd.Series([100.0] * 12)
    assert any(a["period"] == "P7" for a in ie.detect_anomalies(spike, labels))
    assert ie.detect_anomalies(flat, labels) == []


def test_anomaly_detector_needs_enough_history():
    assert ie.detect_anomalies(pd.Series([1.0, 5.0, 1.0]), ["a", "b", "c"]) == []


# --- measure resolution + intent --------------------------------------------------


def test_measure_named_in_the_question_wins():
    source = _source(_orders())
    assert ie.resolve_measure("why did profit fall", source)[0] == "Profit"
    assert ie.resolve_measure("why did revenue fall", source)[0] == "Sales"  # synonym


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Why did our sales decrease last month?", "investigation"),
        ("Why did revenue drop in December?", "investigation"),
        ("What caused the decline in profit?", "investigation"),
        ("What is driving the change in sales?", "investigation"),
        ("Why is Furniture underperforming?", "recommendation"),
        ("What are my total sales?", "simple_aggregation"),
    ],
)
def test_investigation_intent_routing(question, expected):
    assert classify_with_rules(question).intent == expected


def test_false_alarm_rate_on_stable_data_stays_low():
    """Regression test for a real flaw: a 2-sigma rule flagged ~65% of stable datasets.

    Picks the biggest movers out of many segments, so the cut-off must be strict.
    """
    flagged = 0
    seeds = range(12)
    for seed in seeds:
        rng = np.random.default_rng(seed)
        rows = []
        for period in pd.period_range("2024-01", periods=12, freq="M"):
            for region in ["North", "South", "East", "West"]:
                for category in ["Furniture", "Technology", "Office"]:
                    for day in (1, 8, 15, 22, period.days_in_month):
                        rows.append((period.start_time + pd.Timedelta(days=day - 1), region, category,
                                     round(100 + rng.normal(0, 15), 2)))
        frame = pd.DataFrame(rows, columns=["Order Date", "Region", "Category", "Sales"])
        inv = ie.investigate("why did sales change", _source(frame, f"stable{seed}"))
        if any(r.get("unusual") is True for r in list(inv.top_drivers) + list(inv.hotspots)):
            flagged += 1
    assert flagged <= 3, f"{flagged}/12 stable datasets produced a false 'unusual' finding"


def test_false_premise_is_called_out_not_answered_as_asked():
    # steady business that GROWS in the final month; the user asks why it fell
    frame = _orders()
    frame.loc[frame["Order Date"] >= "2024-12-01", "Sales"] *= 1.5
    inv = ie.investigate("Why did our sales decrease last month?", _source(frame, "grew"))
    assert inv.kpis[0]["direction"] == "up"
    assert "did not fall" in inv.summary
    assert inv.headline.startswith("Sales increased")


def test_matching_premise_adds_no_correction():
    inv = ie.investigate("Why did our sales decrease last month?", _source(_orders(collapse=True)))
    assert "did not" not in inv.summary
