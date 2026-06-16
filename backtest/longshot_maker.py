"""Longshot-seller maker backtest.

Each minute, identify the longshot side (cheaper side) and its book ask A.
If A exceeds model fair value F by >= margin, join the ask queue (post-only sell
= maker-buy of the favorite at 1-A). Conservative fill: a later bar must print
THROUGH the level (someone paid more than A). Hold to settlement.
PnL per fill = 1{favorite wins} - (1-A) - maker_fee(1-A).
"""
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # project root for bot.model
from pairdata import load_windows, fee, MAKER_RATE  # noqa: E402
from spot_model import load_spot, realized_vol  # noqa: E402
from bot.model import p_up as deployed_p  # single source of truth  # noqa: E402


@dataclass
class LSParams:
    margin: float = 0.02        # ask premium over model fair required
    min_minute: int = 1
    max_minute: int = 14
    max_ls_price: float = 0.20  # only sell longshots priced <= this
    min_ls_price: float = 0.02
    touch_fill: bool = False
    strike_vol_bps: float = 1.0
    requote: bool = True        # cancel/replace each minute as fair moves


def model_p(sd, vd, open_ts, minute, sv_bps):
    s0 = sd.get(open_ts - 60)
    sm = sd.get(open_ts + 60 * (minute - 1))
    sig = vd.get(open_ts + 60 * (minute - 1))
    if not s0 or not sm or not sig or sig <= 0:
        return None
    tau = 15 - minute
    if tau <= 0:
        return None
    return deployed_p(sm, s0, sig, tau * 60, sv_bps)   # deployed Asian model


def run_ls(df: pd.DataFrame, p: LSParams, assets=("btc", "eth")) -> pd.DataFrame:
    trades = []
    for asset in assets:
        sp = load_spot(asset.upper())
        sd = sp.to_dict()
        vd = realized_vol(sp).to_dict()
        cols = ["open_ts", "minute", f"{asset}_bid", f"{asset}_ask",
                f"{asset}_lo", f"{asset}_hi", f"{asset}_result"]
        sub = df[cols].copy()
        sub.columns = ["open_ts", "minute", "bid", "ask", "lo", "hi", "result"]
        filled = set()
        quotes = {}   # open_ts -> (ls_side, A, fair)
        for r in sub.sort_values(["open_ts", "minute"]).itertuples():
            w = r.open_ts
            if w in filled:
                continue
            q = quotes.get(w)
            if q is not None:
                ls_side, A, _f = q
                eps = 0.0 if p.touch_fill else 0.01
                # trade prints in YES space; longshot ask in its own space
                hit = False
                if ls_side == "yes" and not np.isnan(r.hi) and r.hi >= A + eps:
                    hit = True
                elif ls_side == "no" and not np.isnan(r.lo) and r.lo <= (1 - A) - eps:
                    hit = True
                if hit:
                    fav_side = "no" if ls_side == "yes" else "yes"
                    cost = 1 - A
                    f = fee(cost, rate=MAKER_RATE)
                    won = (r.result == fav_side)
                    trades.append({"open_ts": w, "asset": asset, "minute": r.minute,
                                   "ls_side": ls_side, "ask": A,
                                   "pnl": (1.0 if won else 0.0) - cost - f})
                    filled.add(w)
                    quotes.pop(w, None)
                    continue
            if not (p.min_minute <= r.minute <= p.max_minute - 1):
                quotes.pop(w, None)
                continue
            if np.isnan(r.bid) or np.isnan(r.ask):
                continue
            pu = model_p(sd, vd, w, r.minute, p.strike_vol_bps)
            if pu is None:
                if p.requote:
                    quotes.pop(w, None)
                continue
            # longshot side and its ask
            yes_ask = r.ask
            no_ask = 1 - r.bid
            if yes_ask <= no_ask:
                ls_side, A, fair = "yes", yes_ask, pu
            else:
                ls_side, A, fair = "no", no_ask, 1 - pu
            if not (p.min_ls_price <= A <= p.max_ls_price):
                if p.requote:
                    quotes.pop(w, None)
                continue
            if A - fair >= p.margin:
                quotes[w] = (ls_side, round(A, 2), fair)
            elif p.requote:
                quotes.pop(w, None)
    return pd.DataFrame(trades)


def summarize(tr, df, label=""):
    if len(tr) == 0:
        print(f"{label}: no fills")
        return
    days = max((df.open_ts.max() - df.open_ts.min()) / 86400, 1)
    by_w = tr.groupby("open_ts").pnl.sum()
    t = by_w.mean() / (by_w.std(ddof=1) / np.sqrt(len(by_w)))
    print(f"{label}: fills={len(tr)} ({len(tr)/days:.1f}/d) pnl=${tr.pnl.sum():.2f} "
          f"(${tr.pnl.sum()/days:.2f}/d/contract) avg={tr.pnl.mean()*100:.2f}c "
          f"win%={(tr.pnl > 0).mean():.1%} t={t:.2f}")


if __name__ == "__main__":
    df = load_windows()
    print(f"windows: {df.open_ts.nunique()}")
    for margin in [0.01, 0.02, 0.04]:
        for mx in [0.10, 0.20]:
            tr = run_ls(df, LSParams(margin=margin, max_ls_price=mx))
            summarize(tr, df, f"margin={margin:.2f} max_ls={mx:.2f}")
