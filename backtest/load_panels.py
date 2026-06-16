"""Convert old 1-second panels (Dec 2025 - Mar 2026, trade-derived proxies) into the
same per-minute schema as pairdata.load_windows, cached to data/panels_1m.parquet.

Proxy mapping: ask ~= last aggressive-buy print, bid ~= last aggressive-sell print.
vol columns hold the volume in the 60s before each sample (staleness indicator).
"""
import datetime
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

# Legacy 1-second panel archive from an earlier backtest project. Point this at
# your own export, or set LEGACY_PANEL_DIR in the environment.
OLD = Path(os.getenv("LEGACY_PANEL_DIR", str(Path.home() / "legacy_btc15m_backtest")))
PANELS = OLD / "btc15m_backtest/outputs/eth_btc_15m_all_available_1s/panels"
SETTLE = Path(__file__).resolve().parents[1] / "data" / "pair_settlements.csv"
CACHE = Path(__file__).resolve().parents[1] / "data" / "panels_1m.parquet"

COLS = ["ts_utc", "eth_yes_aggressive_buy", "eth_yes_aggressive_sell", "eth_vol",
        "btc_yes_aggressive_buy", "btc_yes_aggressive_sell", "btc_vol"]


def one_panel(dirname: str):
    f = PANELS / dirname / "aligned_1s.csv"
    if not f.exists():
        return None
    try:
        df = pd.read_csv(f, usecols=COLS)
    except Exception:
        return None
    if len(df) < 300:
        return None
    open_dt = datetime.datetime.strptime(dirname, "%Y_%m_%d_%H%M").replace(
        tzinfo=datetime.timezone.utc)
    open_ts = int(open_dt.timestamp())
    rows = []
    for m in range(1, 16):
        idx = min(m * 60, len(df) - 1)
        r = df.iloc[idx]
        v_btc = df["btc_vol"].iloc[max(0, idx - 59):idx + 1].sum()
        v_eth = df["eth_vol"].iloc[max(0, idx - 59):idx + 1].sum()
        rows.append({
            "open_ts": open_ts, "end_ts": open_ts + m * 60, "minute": m,
            "btc_ask": r["btc_yes_aggressive_buy"], "btc_bid": r["btc_yes_aggressive_sell"],
            "eth_ask": r["eth_yes_aggressive_buy"], "eth_bid": r["eth_yes_aggressive_sell"],
            "btc_vol": v_btc, "eth_vol": v_eth,
        })
    return rows


def build():
    dirs = sorted(p.name for p in PANELS.iterdir() if p.is_dir())
    print(f"{len(dirs)} panels")
    all_rows = []
    with ProcessPoolExecutor(max_workers=8) as ex:
        for i, res in enumerate(ex.map(one_panel, dirs, chunksize=50)):
            if res:
                all_rows.extend(res)
            if (i + 1) % 1000 == 0:
                print(f"{i+1}/{len(dirs)}", flush=True)
    long = pd.DataFrame(all_rows)

    st = pd.read_csv(SETTLE)
    st["open_ts"] = st["open_ts"].astype(int)
    long = long.merge(st[["open_ts", "btc_result", "eth_result"]], on="open_ts", how="inner")

    long["costA"] = long.btc_ask + (1.0 - long.eth_bid)
    long["costB"] = long.eth_ask + (1.0 - long.btc_bid)
    agree = long.btc_result == long.eth_result
    long["payA"] = np.where(agree, 1.0, np.where(long.btc_result == "yes", 2.0, 0.0))
    long["payB"] = np.where(agree, 1.0, np.where(long.eth_result == "yes", 2.0, 0.0))
    long = long.sort_values(["open_ts", "minute"]).reset_index(drop=True)
    long.to_parquet(CACHE)
    print(f"saved {len(long)} rows, {long.open_ts.nunique()} windows -> {CACHE}")


def load() -> pd.DataFrame:
    if not CACHE.exists():
        build()
    return pd.read_parquet(CACHE)


if __name__ == "__main__":
    build()
