"""Synthetic business data with a *known* planted answer, for agent tests."""

from __future__ import annotations

import io

import numpy as np
import pandas as pd

from tests.test_auth import HDR

REGIONS = ["North", "South", "East", "West"]
CATEGORIES = ["Furniture", "Technology", "Office"]
PRODUCTS = {"Furniture": ["Desk", "Chair", "Shelf"], "Technology": ["Phone", "Laptop"], "Office": ["Paper", "Pens"]}


def orders_csv(collapse: tuple | None = ("South", "Furniture"), months: int = 12) -> bytes:
    """Steady business. With `collapse`, that (region, category) falls to 5% in the final month while its discount jumps."""
    rng = np.random.default_rng(11)
    rows = []
    periods = pd.period_range("2024-01", periods=months, freq="M")
    for period in periods:
        last = period == periods[-1]
        for region in REGIONS:
            for category in CATEGORIES:
                hit = collapse == (region, category) and last
                for day in (1, 8, 15, 22, period.days_in_month):
                    for product in PRODUCTS[category]:
                        sales = 100 + rng.normal(0, 3)
                        discount = 0.40 if hit else 0.10
                        if hit:
                            sales *= 0.05
                        rows.append((period.start_time + pd.Timedelta(days=day - 1), region, category, product,
                                     round(sales, 2), round(sales * 0.2, 2), discount))
    frame = pd.DataFrame(rows, columns=["Order Date", "Region", "Category", "Product", "Sales", "Profit", "Discount"])
    return frame.to_csv(index=False).encode()


def inventory_csv(months: int = 12) -> bytes:
    periods = pd.period_range("2024-01", periods=months, freq="M")
    rows = [(p.start_time + pd.Timedelta(days=14), 1000 + 80 * i, "Main") for i, p in enumerate(periods)]
    return pd.DataFrame(rows, columns=["Stock Date", "Inventory Units", "Warehouse"]).to_csv(index=False).encode()


def sales_csv(months: int = 12) -> bytes:
    periods = pd.period_range("2024-01", periods=months, freq="M")
    rows = [(p.start_time + pd.Timedelta(days=14), 5000 - 150 * i + (i % 3) * 40, "Main") for i, p in enumerate(periods)]
    return pd.DataFrame(rows, columns=["Sale Date", "Total Sales", "Store"]).to_csv(index=False).encode()


def upload(client, data: bytes, name: str) -> str:
    r = client.post("/api/upload", data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data", headers=HDR)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["dataset"]["id"]
