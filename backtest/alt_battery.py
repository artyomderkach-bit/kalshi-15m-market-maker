"""Run the full strategy battery on a single (alt) series: calibration, taker
spot-model, longshot maker. Usage: alt_battery.py KXSOL15M sol"""
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # project root for bot.model
from pairdata import load_single, fee, MAKER_RATE  # noqa: E402
from spot_model import load_spot, realized_vol  # noqa: E402
from bot.model import p_up as deployed_p  # single source of truth  # noqa: E402


def add_model(df, asset, strike_vol_bps=1.0):
    sp = load_spot(asset.upper())
    sd = sp.to_dict()
    vd = realized_vol(sp).to_dict()
    ps = []
    for r in df.itertuples():
        s0 = sd.get(r.open_ts - 60)
        sm = sd.get(r.open_ts + 60 * (r.minute - 1))
        sig = vd.get(r.open_ts + 60 * (r.minute - 1))
        tau = 15 - r.minute
        if not s0 or not sm or not sig or sig <= 0 or tau <= 0:
            ps.append(np.nan)
            continue
        ps.append(deployed_p(sm, s0, sig, tau * 60, strike_vol_bps))   # deployed Asian model
    df = df.copy()
    df["p_up"] = ps
    return df


def calibration(df):
    obs = []
    for side in ["yes", "no"]:
        ask = df.ask if side == "yes" else 1 - df.bid
        win = (df.result == side).astype(float)
        obs.append(pd.DataFrame({"open_ts": df.open_ts, "minute": df.minute,
                                 "ask": ask, "win": win}))
    o = pd.concat(obs).dropna(subset=["ask"])
    o = o[(o.minute >= 1) & (o.minute <= 14)]
    o["bucket"] = pd.cut(o.ask, [0.02, 0.10, 0.20, 0.35, 0.50, 0.65, 0.80, 0.90, 0.99])
    o["net"] = o.win - o.ask - o.ask.map(fee)
    rep = []
    for b, g in o.groupby("bucket", observed=True):
        w = g.groupby("open_ts").net.mean()
        if len(w) < 30:
            continue
        rep.append({"bucket": str(b), "n": len(g), "avg_ask": g.ask.mean(),
                    "win": g.win.mean(), "net_ev": w.mean(),
                    "t": w.mean() / (w.std(ddof=1) / np.sqrt(len(w)))})
    return pd.DataFrame(rep)


def taker_model(df, margin):
    d = df.dropna(subset=["p_up", "ask", "bid"])
    d = d[(d.minute >= 2) & (d.minute <= 13)]
    trades = []
    held = set()
    for r in d.sort_values(["open_ts", "minute"]).itertuples():
        if r.open_ts in held:
            continue
        no_ask = 1 - r.bid
        ev_y = r.p_up - r.ask - fee(r.ask)
        ev_n = (1 - r.p_up) - no_ask - fee(no_ask)
        if ev_y > margin and 0 < r.ask < 0.99:
            won = 1.0 if r.result == "yes" else 0.0
            trades.append({"open_ts": r.open_ts, "pnl": won - r.ask - fee(r.ask)})
            held.add(r.open_ts)
        elif ev_n > margin and 0 < no_ask < 0.99:
            won = 1.0 if r.result == "no" else 0.0
            trades.append({"open_ts": r.open_ts, "pnl": won - no_ask - fee(no_ask)})
            held.add(r.open_ts)
    return pd.DataFrame(trades)


def ls_maker(df, margin=0.02, max_ls=0.15, touch=False):
    d = df.sort_values(["open_ts", "minute"])
    filled, quotes, trades = set(), {}, []
    for r in d.itertuples():
        w = r.open_ts
        if w in filled:
            continue
        q = quotes.get(w)
        if q is not None:
            ls_side, A = q
            eps = 0.0 if touch else 0.01
            hit = (ls_side == "yes" and not np.isnan(r.hi) and r.hi >= A + eps) or \
                  (ls_side == "no" and not np.isnan(r.lo) and r.lo <= (1 - A) - eps)
            if hit:
                fav = "no" if ls_side == "yes" else "yes"
                cost = 1 - A
                won = (r.result == fav)
                trades.append({"open_ts": w, "minute": r.minute,
                               "pnl": (1.0 if won else 0.0) - cost - fee(cost, rate=MAKER_RATE)})
                filled.add(w)
                quotes.pop(w, None)
                continue
        if r.minute > 13 or np.isnan(r.bid) or np.isnan(r.ask) or np.isnan(r.p_up):
            quotes.pop(w, None)
            continue
        yes_ask, no_ask = r.ask, 1 - r.bid
        if yes_ask <= no_ask:
            ls_side, A, fair = "yes", yes_ask, r.p_up
        else:
            ls_side, A, fair = "no", no_ask, 1 - r.p_up
        if 0.02 <= A <= max_ls and A - fair >= margin:
            quotes[w] = (ls_side, round(A, 2))
        else:
            quotes.pop(w, None)
    return pd.DataFrame(trades)


def report(tr, df, label):
    if len(tr) == 0:
        print(f"  {label}: no trades")
        return
    days = max((df.open_ts.max() - df.open_ts.min()) / 86400, 1)
    by_w = tr.groupby("open_ts").pnl.sum()
    t = by_w.mean() / (by_w.std(ddof=1) / np.sqrt(len(by_w))) if len(by_w) > 2 else np.nan
    print(f"  {label}: n={len(tr)} ({len(tr)/days:.1f}/d) pnl=${tr.pnl.sum():.2f} "
          f"avg={tr.pnl.mean()*100:.2f}c win%={(tr.pnl > 0).mean():.1%} t={t:.2f}")


def main(series, asset):
    df = load_single(series)
    if len(df) == 0:
        print(f"{series}: no data yet")
        return
    print(f"\n###### {series}: {df.open_ts.nunique()} windows, "
          f"{(df.open_ts.max()-df.open_ts.min())/86400:.0f} days ######")
    vol = df.groupby("open_ts").volume.sum()
    spread = (df.ask - df.bid).median()
    print(f"avg volume/window: {vol.mean():,.0f}  median spread: {spread*100:.1f}c")
    print("--- calibration (taker buy) ---")
    print(calibration(df).to_string(index=False))
    df = add_model(df, asset)
    print("--- taker spot model ---")
    for m in [0.03, 0.06, 0.10]:
        report(taker_model(df, m), df, f"margin={m:.2f}")
    print("--- longshot maker (through-fill) ---")
    for m in [0.01, 0.03]:
        report(ls_maker(df, margin=m), df, f"margin={m:.2f}")
    print("--- longshot maker (touch-fill) ---")
    report(ls_maker(df, margin=0.02, touch=True), df, "margin=0.02")


if __name__ == "__main__":
    series = sys.argv[1] if len(sys.argv) > 1 else "KXSOL15M"
    asset = sys.argv[2] if len(sys.argv) > 2 else series[2:5].lower()
    main(series, asset)
