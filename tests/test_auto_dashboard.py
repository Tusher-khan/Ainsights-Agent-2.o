"""AI auto-analytics dashboard: data-driven chart selection, grounded numbers, filters, exports, isolation."""

from __future__ import annotations

import io
import json

import numpy as np
import pandas as pd

from app.core.datasource import DataSource
from app.dashboard_ai import compose, profiler
from app.files.file_profiler import build_profile
from tests.agent_data import orders_csv
from tests.test_agent_loop import setup_orders
from tests.test_auth import HDR, auth_on, signup


def run(df, name="t", **kw):
    src = DataSource(id="ds1", name=name, kind="dataframe", profile=build_profile(df, name, "csv"))
    return compose.compose(src, df, **kw)


def orders():
    return pd.read_csv(io.BytesIO(orders_csv()))


def kinds(out):
    return {c["kind"] for c in out["charts"]}


def test_kpis_match_pandas_and_discount_is_averaged():
    df = orders(); out = run(df)
    k = {x["measure"]: x for x in out["kpis"] if x["measure"]}
    assert abs(k["Sales"]["value"] - df.Sales.sum()) < 1e-6
    assert k["Discount"]["aggregation"] == "mean" and abs(k["Discount"]["value"] - df.Discount.mean()) < 1e-9
    assert out["kpis"][-1]["value"] == len(df)
    m = df.assign(m=pd.to_datetime(df["Order Date"]).dt.to_period("M")).groupby("m").Sales.sum()
    assert abs(k["Sales"]["change"]["pct"] - round((m.iloc[-1] - m.iloc[-2]) / m.iloc[-2] * 100, 1)) < 0.05
    assert k["Sales"]["sparkline"] and k["Sales"]["how"]["operation"] == "SUM(Sales)"


def test_chart_selection_follows_the_data():
    out = run(orders())
    ks = kinds(out)
    assert {"multi_line", "horizontal_bar", "donut", "histogram"} <= ks
    assert sum(1 for c in out["charts"] if c["role"] == "hero") == 1
    assert out["charts"][0]["role"] == "hero" and out["charts"][0]["kind"] == "multi_line"
    for fig in (c["figure"] for c in out["charts"]):
        assert fig["data"] and fig["layout"]


def test_unsupported_visualizations_are_explained_not_faked():
    out = run(orders())
    by = {u["kind"]: u for u in out["unavailable"]}
    assert {"gauge", "funnel", "sankey"} <= set(by)
    assert "gauge" not in kinds(out) and "funnel" not in kinds(out) and "sankey" not in kinds(out)
    for u in out["unavailable"]:
        assert u["why"] and isinstance(u["try"], list)


def test_waterfall_only_when_revenue_cost_profit_reconcile():
    df = pd.DataFrame({"Order Date": pd.date_range("2024-01-01", periods=120, freq="3D").astype(str), "Region": ["N", "S", "E", "W"] * 30,
                       "Revenue": np.linspace(100, 220, 120)})
    df["Cost"] = df.Revenue * 0.6; df["Profit"] = df.Revenue - df.Cost
    assert "waterfall" in kinds(run(df))
    df["Profit"] = df.Profit * 1.7
    out = run(df)
    assert "waterfall" not in kinds(out) and any(u["kind"] == "waterfall" for u in out["unavailable"])


def test_gauge_only_with_a_target_column():
    df = orders(); df["Sales_Target"] = df.Sales * 1.1
    assert "gauge" in kinds(run(df))


def test_no_date_dataset_has_no_trend_and_says_why():
    df = orders().drop(columns=["Order Date"]); out = run(df)
    assert out["ok"] and not any("line" in c["kind"] or c["kind"] == "area" for c in out["charts"])
    assert any(u["kind"] == "trend" for u in out["unavailable"]) and out["what_changed"] is None
    assert out["charts"][0]["role"] == "hero"  # a ranking becomes the hero


def test_no_category_dataset_explains_composition():
    df = orders()[["Order Date", "Sales", "Profit"]]; out = run(df)
    assert out["ok"] and any(u["kind"] == "composition" for u in out["unavailable"])


def test_high_cardinality_uses_topn_variants_not_a_pie():
    rng = np.random.default_rng(1)
    df = pd.DataFrame({"Customer": [f"C{i%40}" for i in range(400)], "Sales": rng.uniform(10, 200, 400)})
    out = run(df)
    rank = next(c for c in out["charts"] if c["id"].startswith("rank-"))
    assert set(rank["variants"]) == {"top5", "top10", "bottom5", "all"}
    assert len(rank["variants"]["top5"]["data"][0]["x"]) == 5 and len(rank["variants"]["bottom5"]["data"][0]["x"]) == 5
    assert "donut" not in kinds(out)
    vals = df.groupby("Customer").Sales.sum().sort_values(ascending=False)
    assert abs(max(rank["variants"]["top5"]["data"][0]["x"]) - vals.iloc[0]) < 0.01


def test_single_row_and_constant_data_do_not_crash():
    one = pd.DataFrame({"Region": ["N"], "Sales": [5.0]})
    out = run(one)
    assert out["ok"] is False and out["reason"]
    const = pd.DataFrame({"Region": ["N", "S"] * 5, "Sales": [3.0] * 10})
    assert run(const)["ok"] is False


def test_missing_values_are_tolerated_and_reported():
    df = orders(); df.loc[::7, "Sales"] = np.nan
    out = run(df)
    assert out["ok"] and out["quality"]["valid_numeric_pct"] < 100 and out["quality"]["missing_pct"] > 0
    k = next(x for x in out["kpis"] if x["measure"] == "Sales")
    assert abs(k["value"] - df.Sales.sum()) < 1e-6


def test_what_changed_is_grounded_and_never_says_significant():
    df = orders(); out = run(df); w = out["what_changed"]
    assert "10.7%" in w["headline"] and any("South" in i["text"] or "Furniture" in i["text"] for i in w["items"])
    blob = json.dumps(out).lower()
    assert "significant" not in blob


def test_partial_last_month_is_not_reported_as_a_collapse():
    df = orders(); d = pd.to_datetime(df["Order Date"])
    df = df[~((d.dt.month == 12) & (d.dt.day > 5))]
    out = run(df)
    assert "2024-12" not in out["what_changed"]["headline"]


def test_filters_and_date_range_recompute_every_number():
    df = orders()
    out = run(df, filters={"Region": ["South"]})
    assert out["rows"] == (df.Region == "South").sum()
    k = next(x for x in out["kpis"] if x["measure"] == "Sales")
    assert abs(k["value"] - df[df.Region == "South"].Sales.sum()) < 1e-6
    both = run(df, filters={"Region": ["South", "East"]}); assert both["rows"] == df.Region.isin(["South", "East"]).sum()
    ranged = run(df, date_from="2024-07-01", date_to="2024-09-30")
    d = pd.to_datetime(df["Order Date"]); assert ranged["rows"] == ((d >= "2024-07-01") & (d <= "2024-09-30")).sum()
    ghost = run(df, filters={"Nope": ["x"]}); assert ghost["rows"] == len(df)
    empty = run(df, filters={"Region": ["Mars"]}); assert empty["ok"] is False and empty["empty"]


def test_filtering_keeps_the_same_charts_and_chart_ids():
    df = orders(); a = run(df); b = run(df, filters={"Category": ["Office"]})
    assert {c["kind"] for c in b["charts"]} <= {c["kind"] for c in a["charts"]} | {"horizontal_bar"}
    assert b["charts"][0]["id"] == a["charts"][0]["id"]


def test_chart_tables_match_figure_values():
    out = run(orders())
    rank = next(c for c in out["charts"] if c["id"] == "rank-sales-category")
    tab = {r["Category"]: r for r in rank["table"]["rows"]}
    ys = dict(zip(rank["figure"]["data"][0]["y"], rank["figure"]["data"][0]["x"]))
    df = orders().groupby("Category").Sales.sum()
    for k, v in df.items():
        assert abs(ys[k] - v) < 0.01 and k in tab
    assert rank["how"]["operation"].startswith("SUM(Sales)") and rank["how"]["rows"] == len(orders())


def test_ranking_wording_never_calls_the_lowest_leading():
    out = run(orders())
    top = next(i for i in out["insights"] if i["kind"] == "top"); low = next(i for i in out["insights"] if i["kind"] == "attention")
    assert "largest" in top["text"] and "lowest" in low["text"] and "leading" not in low["text"].lower()


def test_correlation_only_with_enough_support():
    df = orders().head(20); out = run(df)
    assert "scatter" not in kinds(out)


def test_aggregation_rules_in_profile():
    p = profiler.build(orders(), DataSource(id="x", name="x", kind="dataframe", profile=build_profile(orders(), "x", "csv")))
    roles = {m.name: (m.role, m.agg) for m in p.measures}
    assert roles["Sales"] == ("revenue", "sum") and roles["Discount"][1] == "mean"


# ------------------------------------------------------------------------------------------------- API
def make(client, ds):
    r = client.post("/api/dashboards", json={"dataset_id": ds}, headers=HDR)
    assert r.status_code == 201
    return r.get_json()["dashboard"]["id"]


def test_api_payload_filters_parts_and_export():
    with auth_on():
        client, ds = setup_orders()
        d = make(client, ds)
        full = client.get(f"/api/dashboards/{d}/auto").get_json()
        assert full["ok"] and full["charts"] and full["tabs"] and full["story"]["sections"]
        part = client.get(f"/api/dashboards/{d}/auto?part=core").get_json()
        assert len(part["charts"]) == 1 and part["charts"][0]["role"] == "hero"
        rest = client.get(f"/api/dashboards/{d}/auto?part=rest").get_json()
        assert all(c["role"] != "hero" for c in rest["charts"])
        f = client.get(f"/api/dashboards/{d}/auto?f_Region=South").get_json()
        assert f["rows"] < full["rows"] and f["applied_filters"] == {"Region": ["South"]}
        csv = client.get(f"/api/dashboards/{d}/export.csv?f_Region=South")
        assert csv.status_code == 200 and csv.data.decode().count("\n") - 1 == f["rows"]
        x = client.get(f"/api/dashboards/{d}/export.xlsx"); assert x.data[:2] == b"PK"
        assert client.get(f"/api/dashboards/{d}/export.pdf").status_code >= 400


def test_layout_validation_duplicate_and_refresh():
    with auth_on():
        client, ds = setup_orders()
        d = make(client, ds)
        put = lambda body: client.put(f"/api/dashboards/{d}/layout", json=body, headers=HDR)
        assert put({"hidden": ["trend-sales"], "favorites": ["a"], "sizes": {"a": "large"}}).status_code == 200
        assert put({"sizes": {"a": "huge"}}).status_code >= 400
        assert put({"hidden": "x"}).status_code >= 400
        assert put({"measure": "NotAColumn"}).status_code >= 400
        assert put({"saved_filters": [{"name": "", "filters": {}}]}).status_code >= 400
        assert put({"saved_filters": [{"name": "South", "filters": {"Region": ["South"]}}]}).status_code == 200
        v = client.get(f"/api/dashboards/{d}/auto").get_json()
        assert v["layout"]["hidden"] == ["trend-sales"] and v["layout"]["saved_filters"][0]["name"] == "South"
        dup = client.post(f"/api/dashboards/{d}/duplicate", json={}, headers=HDR)
        assert dup.status_code == 201 and dup.get_json()["dashboard"]["id"] != d
        r = client.post(f"/api/dashboards/{d}/refresh", json={}, headers=HDR)
        assert r.status_code == 200 and r.get_json()["ok"] and not r.get_json()["cached"]
        assert client.get(f"/api/dashboards/{d}/auto").get_json()["cached"] is True


def test_export_neutralises_spreadsheet_formulas():
    from app.services.auto_dashboard_service import _safe_cell
    assert _safe_cell("=1+1").startswith("'") and _safe_cell("@x").startswith("'") and _safe_cell("ok") == "ok"


def test_auto_dashboard_is_isolated_between_users():
    with auth_on():
        client, ds = setup_orders()
        d = make(client, ds)
        client.get(f"/api/dashboards/{d}/auto")  # warm the cache for the owner
        other = client.application.test_client(); signup(other, "auto-intruder@example.com")
        for url in (f"/api/dashboards/{d}/auto", f"/api/dashboards/{d}/export.csv"):
            assert other.get(url).status_code >= 400
        for method, url in (("post", f"/api/dashboards/{d}/refresh"), ("post", f"/api/dashboards/{d}/duplicate"), ("put", f"/api/dashboards/{d}/layout")):
            assert getattr(other, method)(url, json={}, headers=HDR).status_code >= 400
