"""Spot-lead fair-value strategy backtest.

Fair value comes from bot.model.p_up, imported as the single source of truth so
the backtest and the engine never drift apart. Trade (taker) when the model edge
vs the Kalshi ask exceeds margin after fees; hold to settle.
"""
import math
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # project root for bot.model
from pairdata import load_windows, fee  # noqa: E402
from bot.model import p_up as deployed_p  # single source of truth  # noqa: E402

SPOT_DB = Path(__file__).resolve().parents[1] / "data" / "spot.db"


def load_spot(symbol: str) -> pd.Series:
    con = sqlite3.connect(str(SPOT_DB))
    df = pd.read_sql_query(
        f"SELECT ts, close FROM spot WHERE symbol='{symbol}' ORDER BY ts", con)
    con.close()
    return pd.Series(df.close.values, index=df.ts.values)


def realized_vol(close: pd.Series, lookback=120) -> pd.Series:
    """Rolling std of 1-min log returns, indexed by bar ts (per-minute sigma)."""
    lr = np.log(close).diff()
    return lr.rolling(lookback, min_periods=60).std()


@dataclass
class SpotParams:
    margin: float = 0.05        # required model edge after fees
    min_minute: int = 2
    max_minute: int = 13
    strike_vol_bps: float = 1.0  # strike/index uncertainty in bps of price
    max_p: float = 0.97          # don't buy beyond this model prob
    one_shot: bool = True        # one position per window-asset


def run_spot_strategy(df: pd.DataFrame, p: SpotParams):
    spot = {a: load_spot(a.upper()) for a in ["btc", "eth"]}
    vol = {a: realized_vol(spot[a]) for a in spot}
    spot_d = {a: s.to_dict() for a, s in spot.items()}
    vol_d = {a: v.to_dict() for a, v in vol.items()}

    trades = []
    for asset in ["btc", "eth"]:
        sd, vd = spot_d[asset], vol_d[asset]
        sub = df[["open_ts", "minute", f"{asset}_ask", f"{asset}_bid",
                  f"{asset}_result"]].dropna()
        sub = sub[(sub.minute >= p.min_minute) & (sub.minute <= p.max_minute)]
        held = set()
        for r in sub.itertuples():
            if p.one_shot and r.open_ts in held:
                continue
            # strike: close of the bar ENDING at open_ts (spot at window open)
            s0 = sd.get(r.open_ts - 60)
            sm = sd.get(r.open_ts + 60 * (r.minute - 1))  # bar ending at sample time
            sig1m = vd.get(r.open_ts + 60 * (r.minute - 1))
            if not s0 or not sm or not sig1m or sig1m <= 0:
                continue
            tau_min = 15 - r.minute
            if tau_min <= 0:
                continue
            p_up = deployed_p(sm, s0, sig1m, tau_min * 60, p.strike_vol_bps)

            ask_yes = getattr(r, f"{asset}_ask")
            ask_no = 1.0 - getattr(r, f"{asset}_bid")
            won_yes = 1.0 if getattr(r, f"{asset}_result") == "yes" else 0.0

            ev_yes = p_up - ask_yes - fee(ask_yes)
            ev_no = (1 - p_up) - ask_no - fee(ask_no)
            side = None
            if ev_yes > p.margin and p_up <= p.max_p and 0 < ask_yes < 1:
                side, cost, pay = "yes", ask_yes + fee(ask_yes), won_yes
            elif ev_no > p.margin and (1 - p_up) <= p.max_p and 0 < ask_no < 1:
                side, cost, pay = "no", ask_no + fee(ask_no), 1.0 - won_yes
            if side:
                held.add(r.open_ts)
                trades.append({"open_ts": r.open_ts, "asset": asset, "minute": r.minute,
                               "side": side, "cost": cost, "pay": pay,
                               "pnl": pay - cost, "model_p": p_up if side == "yes" else 1 - p_up,
                               "ev": ev_yes if side == "yes" else ev_no})
    return pd.DataFrame(trades)


def summarize(tr: pd.DataFrame, label=""):
    if len(tr) == 0:
        print(f"{label}: no trades")
        return
    days = max((tr.open_ts.max() - tr.open_ts.min()) / 86400, 1)
    by_w = tr.groupby("open_ts").pnl.sum()
    t = by_w.mean() / (by_w.std(ddof=1) / np.sqrt(len(by_w))) if len(by_w) > 2 else np.nan
    print(f"{label}: {len(tr)} trades in {tr.open_ts.nunique()} windows over {days:.0f}d | "
          f"pnl=${tr.pnl.sum():.2f} (${tr.pnl.sum()/days:.2f}/d/contract) | "
          f"win%={(tr.pnl > 0).mean():.1%} avg={tr.pnl.mean()*100:.2f}c | t={t:.2f}")


def main():
    if "--panels" in sys.argv:
        from load_panels import load
        df = load()
    else:
        df = load_windows()
    for margin in [0.03, 0.05, 0.08, 0.12]:
        tr = run_spot_strategy(df, SpotParams(margin=margin))
        summarize(tr, f"margin={margin:.2f}")


if __name__ == "__main__":
    main()
