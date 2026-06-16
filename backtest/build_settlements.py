"""Build pair settlement table from the old project's cache.db + matched_pairs.csv.

Output: data/pair_settlements.csv with one row per matched BTC/ETH 15m window,
including settlement results for both legs.
"""
import json
import os
import sqlite3
from pathlib import Path

import pandas as pd

# Legacy 1-second panel archive from an earlier backtest project. Point this at
# your own export, or set LEGACY_PANEL_DIR in the environment.
OLD = Path(os.getenv("LEGACY_PANEL_DIR", str(Path.home() / "legacy_btc15m_backtest")))
CACHE_DB = OLD / "cache.db"
PAIRS_CSV = OLD / "btc15m_backtest/outputs/eth_btc_15m_combined_available_1s/markets/matched_pairs.csv"
OUT = Path(__file__).resolve().parents[1] / "data" / "pair_settlements.csv"


def main():
    pairs = pd.read_csv(PAIRS_CSV)
    con = sqlite3.connect(str(CACHE_DB))
    rows = con.execute("SELECT ticker, data FROM markets").fetchall()
    con.close()

    results = {}
    for ticker, blob in rows:
        d = json.loads(blob)
        results[ticker] = d.get("result")

    pairs["btc_result"] = pairs["btc_ticker"].map(results)
    pairs["eth_result"] = pairs["eth_ticker"].map(results)
    pairs = pairs.dropna(subset=["btc_result", "eth_result"])
    pairs = pairs[pairs["btc_result"].isin(["yes", "no"]) & pairs["eth_result"].isin(["yes", "no"])]
    pairs["agree"] = pairs["btc_result"] == pairs["eth_result"]

    OUT.parent.mkdir(exist_ok=True)
    pairs.to_csv(OUT, index=False)

    n = len(pairs)
    print(f"pairs with both settlements: {n}")
    print(f"agreement rate: {pairs['agree'].mean():.4f}")
    print(pairs.groupby(['btc_result', 'eth_result']).size())
    # date range
    print("range:", pairs["window_utc"].min(), "->", pairs["window_utc"].max())


if __name__ == "__main__":
    main()
