"""Informed-maker backtest: rest a bid on the model-favored side, capture longshot flow.

Mechanics per window-asset:
  - each minute, compute model p_up from spot; favorite side f (p_fav = max(p, 1-p))
  - quote: resting buy of favorite at L = floor_cent(p_fav - margin), post-only
    (skip if L would cross the book)
  - fill model: order placed using bar-m close info is live during bar m+1;
    CONSERVATIVE fill requires a trade to print THROUGH the level:
      YES buy at L: bar price_low  <= L - 0.01
      NO  buy at q: bar price_high >= 1 - q + 0.01
  - first fill wins; hold to settlement; maker fee 0.0175.
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
class MakerParams:
    margin: float = 0.04          # quote this far below model fair value
    min_minute: int = 1
    max_minute: int = 14          # last bar during which a fill may occur
    min_p_fav: float = 0.55       # only quote when model has a favorite
    max_price: float = 0.97
    min_price: float = 0.40
    touch_fill: bool = False      # True = optimistic touch fills
    strike_vol_bps: float = 1.0
    vol_lookback: int = 120


def model_p_up(sd, vd, open_ts, minute, p: MakerParams):
    s0 = sd.get(open_ts - 60)
    sm = sd.get(open_ts + 60 * (minute - 1))
    sig = vd.get(open_ts + 60 * (minute - 1))
    if not s0 or not sm or not sig or sig <= 0:
        return None
    tau = 15 - minute
    if tau <= 0:
        return None
    return deployed_p(sm, s0, sig, tau * 60, p.strike_vol_bps)   # deployed Asian model


def run_maker(df: pd.DataFrame, p: MakerParams) -> pd.DataFrame:
    spot = {a: load_spot(a.upper()) for a in ["btc", "eth"]}
    sd = {a: s.to_dict() for a, s in spot.items()}
    vd = {a: realized_vol(s, p.vol_lookback).to_dict() for a, s in spot.items()}

    trades = []
    for asset in ["btc", "eth"]:
        cols = ["open_ts", "minute", f"{asset}_bid", f"{asset}_ask",
                f"{asset}_lo", f"{asset}_hi", f"{asset}_result"]
        sub = df[cols].copy()
        sub.columns = ["open_ts", "minute", "bid", "ask", "lo", "hi", "result"]
        filled = set()
        # active quote per window: (side, price, placed_minute)
        quotes = {}
        for r in sub.sort_values(["open_ts", "minute"]).itertuples():
            w = r.open_ts
            if w in filled:
                continue
            # 1) check fill of an existing quote against THIS bar's prints
            q = quotes.get(w)
            if q is not None:
                side, L, _m = q
                eps = 0.0 if p.touch_fill else 0.01
                hit = False
                if side == "yes" and not np.isnan(r.lo) and r.lo <= L - eps:
                    hit = True
                elif side == "no" and not np.isnan(r.hi) and r.hi >= (1 - L) + eps:
                    hit = True
                if hit:
                    won = (r.result == side)
                    f = fee(L, rate=MAKER_RATE)
                    trades.append({"open_ts": w, "asset": asset, "minute": r.minute,
                                   "side": side, "price": L,
                                   "pnl": (1.0 if won else 0.0) - L - f})
                    filled.add(w)
                    quotes.pop(w, None)
                    continue
            # 2) (re)quote using this bar's close info
            if not (p.min_minute <= r.minute <= p.max_minute - 1):
                quotes.pop(w, None)
                continue
            pu = model_p_up(sd[asset], vd[asset], w, r.minute, p)
            if pu is None:
                quotes.pop(w, None)
                continue
            side = "yes" if pu >= 0.5 else "no"
            p_fav = max(pu, 1 - pu)
            if p_fav < p.min_p_fav:
                quotes.pop(w, None)
                continue
            L = math.floor((p_fav - p.margin) * 100) / 100.0
            L = min(L, p.max_price)
            if L < p.min_price:
                quotes.pop(w, None)
                continue
            # post-only: don't cross the book
            if side == "yes":
                if not np.isnan(r.ask) and L >= r.ask:
                    L = r.ask - 0.01
            else:
                no_ask = 1 - r.bid if not np.isnan(r.bid) else None
                if no_ask is not None and L >= no_ask:
                    L = no_ask - 0.01
            if L < p.min_price:
                quotes.pop(w, None)
                continue
            quotes[w] = (side, round(L, 2), r.minute)
    return pd.DataFrame(trades)


def summarize(tr: pd.DataFrame, df, label=""):
    if len(tr) == 0:
        print(f"{label}: no fills")
        return
    days = max((df.open_ts.max() - df.open_ts.min()) / 86400, 1)
    by_w = tr.groupby("open_ts").pnl.sum()
    t = by_w.mean() / (by_w.std(ddof=1) / np.sqrt(len(by_w)))
    print(f"{label}: fills={len(tr)} ({len(tr)/days:.1f}/day) | pnl=${tr.pnl.sum():.2f} "
          f"(${tr.pnl.sum()/days:.2f}/day/contract) | avg={tr.pnl.mean()*100:.2f}c | "
          f"win%={(tr.pnl > 0).mean():.1%} | t={t:.2f}")


def main():
    df = load_windows()
    print(f"windows: {df.open_ts.nunique()}")
    for margin in [0.02, 0.04, 0.06, 0.10]:
        tr = run_maker(df, MakerParams(margin=margin))
        summarize(tr, df, f"margin={margin:.2f} through-fill")
    tr = run_maker(df, MakerParams(margin=0.04, touch_fill=True))
    summarize(tr, df, "margin=0.04 touch-fill (optimistic)")


if __name__ == "__main__":
    main()
