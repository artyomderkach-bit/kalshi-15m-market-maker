"""Empirical EV of buying the BTC/ETH pair vs combined cost.

For every (window, minute) we observe two tradeable pairs:
  A: BTC-YES + ETH-NO  -> costA = btc_ask + (1-eth_bid), pays $1 agree / $2 btc-only-up / $0 eth-only-up
  B: ETH-YES + BTC-NO  -> symmetric

This measures, with no strategy parameters, whether a cheap pair (<$1) is actually
mispriced or just predicting divergence (adverse selection).
SEs are clustered by window (payoffs within a window are perfectly correlated).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pairdata import load_windows, fee  # noqa: E402


def pair_obs(df: pd.DataFrame) -> pd.DataFrame:
    a = df[["open_ts", "minute", "costA", "payA", "btc_ask", "eth_bid", "btc_vol", "eth_vol"]].copy()
    a.columns = ["open_ts", "minute", "cost", "pay", "leg1_px", "leg2_bid", "v1", "v2"]
    a["pair"] = "A"
    b = df[["open_ts", "minute", "costB", "payB", "eth_ask", "btc_bid", "eth_vol", "btc_vol"]].copy()
    b.columns = ["open_ts", "minute", "cost", "pay", "leg1_px", "leg2_bid", "v1", "v2"]
    b["pair"] = "B"
    obs = pd.concat([a, b], ignore_index=True).dropna(subset=["cost", "pay"])
    # entry fees: taker on both legs (1 contract each)
    obs["fees"] = obs.apply(lambda r: fee(r.leg1_px) + fee(1.0 - r.leg2_bid), axis=1)
    obs["net"] = obs.pay - obs.cost - obs.fees
    return obs


def clustered_mean_se(g: pd.DataFrame, col="net"):
    """Mean and SE of col, clustering on open_ts (one obs per window = its mean)."""
    w = g.groupby("open_ts")[col].mean()
    return w.mean(), w.std(ddof=1) / np.sqrt(len(w)), len(w)


def bucket_report(obs: pd.DataFrame, edges=None, min_minute=1, max_minute=15):
    if edges is None:
        edges = [0.0, 0.70, 0.80, 0.85, 0.88, 0.90, 0.92, 0.94, 0.96, 0.98, 1.00, 1.05, 9.9]
    o = obs[(obs.minute >= min_minute) & (obs.minute <= max_minute)].copy()
    o["bucket"] = pd.cut(o.cost, edges)
    rows = []
    for b, g in o.groupby("bucket", observed=True):
        if len(g) == 0:
            continue
        m, se, nw = clustered_mean_se(g)
        rows.append({"bucket": str(b), "n_obs": len(g), "n_windows": nw,
                     "avg_cost": g.cost.mean(), "avg_pay": g.pay.mean(),
                     "net_ev": m, "se": se, "t": m / se if se > 0 else np.nan})
    return pd.DataFrame(rows)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--panels" in sys.argv:
        from load_panels import load
        df = load()
        if len(args) >= 1:
            df = df[df.open_ts >= int(args[0])]
        if len(args) >= 2:
            df = df[df.open_ts < int(args[1])]
    else:
        min_ts = int(args[0]) if len(args) > 0 else None
        max_ts = int(args[1]) if len(args) > 1 else None
        df = load_windows(min_ts, max_ts)
    print(f"windows: {df.open_ts.nunique()}, rows: {len(df)}")
    obs = pair_obs(df)
    print(f"pair observations: {len(obs)}")

    print("\n=== ALL minutes 1-13 (entry window) ===")
    print(bucket_report(obs, min_minute=1, max_minute=13).to_string(index=False))

    print("\n=== Early (minutes 1-5) ===")
    print(bucket_report(obs, min_minute=1, max_minute=5).to_string(index=False))

    print("\n=== Mid (minutes 6-10) ===")
    print(bucket_report(obs, min_minute=6, max_minute=10).to_string(index=False))

    print("\n=== Late (minutes 11-13) ===")
    print(bucket_report(obs, min_minute=11, max_minute=13).to_string(index=False))

    # how often is a cheap pair even available?
    cheap = obs[(obs.cost <= 0.93) & (obs.minute <= 13)]
    print(f"\ncheap (<=93c) availability: {cheap.open_ts.nunique()} of {obs.open_ts.nunique()} windows")


if __name__ == "__main__":
    main()
