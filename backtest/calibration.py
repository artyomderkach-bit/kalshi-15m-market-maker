"""Calibration scan: is any (price, minute) pocket mispriced after fees?

For each leg (BTC, ETH), each minute, each ask-price bucket:
  net EV of taker-buying YES  = P(yes | ask bucket, minute) - avg_ask - fee
  net EV of taker-buying NO   = P(no | no_ask bucket, minute) - avg_no_ask - fee
SEs clustered by window.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pairdata import load_windows, fee  # noqa: E402


def leg_obs(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for asset in ["btc", "eth"]:
        for side in ["yes", "no"]:
            if side == "yes":
                ask = df[f"{asset}_ask"]
                win = (df[f"{asset}_result"] == "yes").astype(float)
            else:
                ask = 1.0 - df[f"{asset}_bid"]
                win = (df[f"{asset}_result"] == "no").astype(float)
            o = pd.DataFrame({"open_ts": df.open_ts, "minute": df.minute,
                              "ask": ask, "win": win})
            o["asset"] = asset
            o["side"] = side
            rows.append(o.dropna(subset=["ask"]))
    return pd.concat(rows, ignore_index=True)


def scan(obs: pd.DataFrame, price_edges=None, minute_bins=None):
    if price_edges is None:
        price_edges = list(np.round(np.arange(0.02, 1.0, 0.07), 2)) + [0.99]
    if minute_bins is None:
        minute_bins = [(1, 3), (4, 6), (7, 9), (10, 12), (13, 15)]
    out = []
    for lo_m, hi_m in minute_bins:
        sl = obs[(obs.minute >= lo_m) & (obs.minute <= hi_m)].copy()
        sl["bucket"] = pd.cut(sl.ask, price_edges)
        for b, g in sl.groupby("bucket", observed=True):
            if g.open_ts.nunique() < 30:
                continue
            g = g.assign(net=g.win - g.ask - g.ask.map(fee))
            w = g.groupby("open_ts").net.mean()
            m, se = w.mean(), w.std(ddof=1) / np.sqrt(len(w))
            out.append({"minutes": f"{lo_m}-{hi_m}", "bucket": str(b),
                        "n_obs": len(g), "n_w": len(w),
                        "avg_ask": g.ask.mean(), "win_rate": g.win.mean(),
                        "net_ev": m, "se": se, "t": m / se if se > 0 else np.nan})
    return pd.DataFrame(out)


def main():
    if "--panels" in sys.argv:
        from load_panels import load
        df = load()
    else:
        df = load_windows()
    obs = leg_obs(df)
    rep = scan(obs)
    rep = rep.sort_values("t", ascending=False)
    print("=== TOP 15 by t-stat (taker buy) ===")
    print(rep.head(15).to_string(index=False))
    print("\n=== BOTTOM 10 ===")
    print(rep.tail(10).to_string(index=False))
    pos = rep[(rep.net_ev > 0) & (rep.t > 2)]
    print(f"\nbuckets with net_ev>0 and t>2: {len(pos)} of {len(rep)}")


if __name__ == "__main__":
    main()
