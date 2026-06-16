"""Strategy simulator for the BTC/ETH pair-arb on 1-min candle closes.

Replicates the video-bot mechanics: activation delay, entry cost range, shots,
cooldown, no-entry cutoff, optional pair stop-loss; taker entries; hold to settlement.
"""
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pairdata import load_windows, fee  # noqa: E402


@dataclass
class Params:
    entry_lo: float = 0.80
    entry_hi: float = 0.93
    activate_min: int = 1
    no_entry_after: int = 13
    shots: int = 5
    cooldown: int = 1          # minutes between entries
    stop_level: float = 0.0    # liquidate if pair sell-value < stop (0 = off)
    size: int = 1              # contracts per leg per shot


def simulate_window(g: pd.DataFrame, p: Params):
    """g: one window's rows sorted by minute. Returns (pnl, n_entries, info)."""
    side = None          # 'A' or 'B' once locked
    entries = []         # entry costs
    last_entry_min = -99
    pnl = 0.0
    stopped = False

    for row in g.itertuples():
        m = row.minute
        if side is not None and p.stop_level > 0 and entries and not stopped:
            if side == "A":
                sell_val = (row.btc_bid or 0) + (1.0 - (row.eth_ask or 1))
                exit_fees = fee(row.btc_bid, p.size) + fee(1.0 - row.eth_ask, p.size)
            else:
                sell_val = (row.eth_bid or 0) + (1.0 - (row.btc_ask or 1))
                exit_fees = fee(row.eth_bid, p.size) + fee(1.0 - row.btc_ask, p.size)
            if not np.isnan(sell_val) and sell_val < p.stop_level:
                pnl += len(entries) * p.size * sell_val - len(entries) * exit_fees
                pnl -= sum(c * p.size for c, _f in entries) + sum(f for _c, f in entries)
                return pnl, len(entries), "stopped"

        if stopped or m < p.activate_min or m > p.no_entry_after:
            continue
        if len(entries) >= p.shots or (m - last_entry_min) < p.cooldown:
            continue

        ca, cb = row.costA, row.costB
        cand = []
        if side in (None, "A") and not np.isnan(ca) and p.entry_lo <= ca <= p.entry_hi:
            cand.append(("A", ca, fee(row.btc_ask, p.size) + fee(1.0 - row.eth_bid, p.size)))
        if side in (None, "B") and not np.isnan(cb) and p.entry_lo <= cb <= p.entry_hi:
            cand.append(("B", cb, fee(row.eth_ask, p.size) + fee(1.0 - row.btc_bid, p.size)))
        if not cand:
            continue
        cand.sort(key=lambda x: x[1])
        s, c, fe = cand[0]
        side = s
        entries.append((c, fe))
        last_entry_min = m

    if not entries:
        return 0.0, 0, "none"
    payoff = g.iloc[0].payA if side == "A" else g.iloc[0].payB
    pnl = sum(p.size * (payoff - c) - f for c, f in entries)
    return pnl, len(entries), "settled"


def run(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    out = []
    for ts, g in df.groupby("open_ts"):
        g = g.sort_values("minute")
        pnl, n, status = simulate_window(g, p)
        if n > 0:
            out.append({"open_ts": ts, "pnl": pnl, "entries": n, "status": status})
    return pd.DataFrame(out)


def summarize(res: pd.DataFrame, label="", windows_total=0):
    if len(res) == 0:
        print(f"{label}: no trades")
        return {}
    pnl = res.pnl
    days = max((res.open_ts.max() - res.open_ts.min()) / 86400.0, 1.0)
    s = {
        "label": label,
        "windows_traded": len(res),
        "windows_total": windows_total,
        "total_entries": int(res.entries.sum()),
        "total_pnl": pnl.sum(),
        "pnl_per_window": pnl.mean(),
        "win_rate": (pnl > 0).mean(),
        "stop_rate": (res.status == "stopped").mean(),
        "pnl_per_day": pnl.sum() / days,
        "sharpe_window": pnl.mean() / pnl.std(ddof=1) if pnl.std(ddof=1) > 0 else np.nan,
        "max_dd": (pnl.cumsum().cummax() - pnl.cumsum()).max(),
        "days": days,
    }
    print(f"{label}: traded {s['windows_traded']}/{windows_total}w "
          f"entries={s['total_entries']} pnl=${s['total_pnl']:.2f} "
          f"(${s['pnl_per_day']:.2f}/day) win%={s['win_rate']:.2%} "
          f"stop%={s['stop_rate']:.1%} maxDD=${s['max_dd']:.2f} "
          f"shrp/w={s['sharpe_window']:.3f}")
    return s


if __name__ == "__main__":
    df = load_windows()
    p = Params()
    res = run(df, p)
    summarize(res, "default", df.open_ts.nunique())
