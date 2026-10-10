"""Regenerate the demonstration CSV files in sample_data/ (deterministic).

    python scripts/generate_sample_data.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parents[1] / "sample_data"


def branch_sales_upload(rng: np.random.Generator) -> pd.DataFrame:
    branches = ["Camden", "Canary Wharf", "Digbeth", "Edgbaston", "Northern Quarter"]
    categories = ["Coffee", "Dairy Alt", "Bakery", "Tea", "Merchandise", "Syrups"]
    rows = []
    for day in pd.date_range("2026-03-01", "2026-08-31", freq="D"):
        for b_i, branch in enumerate(branches, start=1):
            for cat in categories:
                base = 120 + 25 * b_i + (40 if cat == "Coffee" else 0)
                season = 1 + 0.15 * np.sin(2 * np.pi * day.dayofyear / 365)
                revenue = round(base * season * rng.uniform(0.7, 1.3), 2)
                rows.append({
                    "transaction_date": day.strftime("%d/%m/%Y"),  # UK format on purpose
                    "branch_id": b_i, "branch": branch, "category": cat,
                    "units": int(revenue // rng.uniform(3, 6)), "revenue_gbp": revenue,
                    "currency": "GBP",
                })
    df = pd.DataFrame(rows)
    # Deliberate quality issues for the profiler to find.
    df.loc[df.sample(40, random_state=1).index, "revenue_gbp"] = np.nan
    df.loc[df.sample(25, random_state=2).index, "branch"] = "camden "
    df.loc[df.sample(10, random_state=3).index, "branch"] = "CAMDEN"
    df.loc[5, "transaction_date"] = "31/02/2026"
    df.loc[6, "transaction_date"] = "not recorded"
    df.loc[7, "units"] = -12
    df.loc[8, "revenue_gbp"] = 98_765.43
    df.loc[df.sample(12, random_state=4).index, "currency"] = "EUR"
    df = pd.concat([df, df.iloc[100:108]], ignore_index=True)  # duplicate rows
    return df


def branch_targets() -> pd.DataFrame:
    return pd.DataFrame({
        "branch_id": [1, 2, 3, 4, 5, 5, 9],
        "branch_name": ["Camden", "Canary Wharf", "Digbeth", "Edgbaston", "Northern Quarter",
                        "Northern Quarter", "Leeds (planned)"],
        "monthly_target_gbp": [52000, 61000, 48000, 39000, 45000, 46000, 30000],
        "target_year": [2026] * 7,
    })


def main() -> None:
    OUT.mkdir(exist_ok=True)
    rng = np.random.default_rng(42)
    branch_sales_upload(rng).to_csv(OUT / "branch_sales_upload.csv", index=False)
    branch_targets().to_csv(OUT / "branch_targets.csv", index=False)
    print("Wrote", *(p.name for p in OUT.glob("*.csv")))


if __name__ == "__main__":
    main()
