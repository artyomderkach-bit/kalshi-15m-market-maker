"""Load paired BTC/ETH 15m windows from data/market_data.db into per-window frames."""
import math
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

DB = Path(__file__).resolve().parents[1] / "data" / "market_data.db"

TAKER_RATE = 0.07
MAKER_RATE = 0.0175


def fee(price: float, contracts: float = 1.0, rate: float = TAKER_RATE) -> float:
    """Kalshi fee in dollars, rounded UP to next cent per execution."""
    raw = rate * contracts * price * (1.0 - price)
    return math.ceil(raw * 100.0 - 1e-9) / 100.0


def load_windows(min_open_ts=None, max_open_ts=None) -> pd.DataFrame:
    """Return one long DataFrame: one row per (window, minute) with both legs' quotes.

    Columns: open_ts, close_ts, minute (1..15), btc_bid, btc_ask, eth_bid, eth_ask,
    btc_vol, eth_vol, btc_result, eth_result, btc_ticker, eth_ticker.
    Quotes are minute-bar closes (dollars).
    """
    con = sqlite3.connect(str(DB))
    where = "WHERE result IN ('yes','no')"
    if min_open_ts:
        where += f" AND open_ts >= {int(min_open_ts)}"
    if max_open_ts:
        where += f" AND open_ts < {int(max_open_ts)}"
    mk = pd.read_sql_query(f"SELECT * FROM markets {where}", con)
    btc = mk[mk.series == "KXBTC15M"][["ticker", "open_ts", "close_ts", "result"]]
    eth = mk[mk.series == "KXETH15M"][["ticker", "open_ts", "close_ts", "result"]]
    pairs = btc.merge(eth, on=["open_ts", "close_ts"], suffixes=("_btc", "_eth"))

    tickers = pd.concat([pairs.ticker_btc, pairs.ticker_eth])
    ph = ",".join("?" * 0)  # noqa: F841
    cd = pd.read_sql_query(
        "SELECT ticker, end_ts, bid_close, ask_close, price_low, price_high, volume "
        "FROM candles", con)
    con.close()
    cd = cd[cd.ticker.isin(set(tickers))]

    b = cd.rename(columns={"bid_close": "btc_bid", "ask_close": "btc_ask",
                           "price_low": "btc_lo", "price_high": "btc_hi",
                           "volume": "btc_vol"})
    e = cd.rename(columns={"bid_close": "eth_bid", "ask_close": "eth_ask",
                           "price_low": "eth_lo", "price_high": "eth_hi",
                           "volume": "eth_vol"})

    rows = pairs.rename(columns={"ticker_btc": "btc_ticker", "ticker_eth": "eth_ticker",
                                 "result_btc": "btc_result", "result_eth": "eth_result"})

    long = rows.merge(b, left_on="btc_ticker", right_on="ticker", how="left").drop(columns=["ticker"])
    e2 = e[["ticker", "end_ts", "eth_bid", "eth_ask", "eth_lo", "eth_hi", "eth_vol"]]
    long = long.merge(e2, left_on=["eth_ticker", "end_ts"], right_on=["ticker", "end_ts"],
                      how="inner").drop(columns=["ticker"])
    long["minute"] = ((long.end_ts - long.open_ts) / 60).round().astype(int)
    long = long[(long.minute >= 1) & (long.minute <= 15)]

    # pair costs (taker, buying both legs at ask; NO ask = 1 - YES bid)
    long["costA"] = long.btc_ask + (1.0 - long.eth_bid)   # BTC-YES + ETH-NO
    long["costB"] = long.eth_ask + (1.0 - long.btc_bid)   # ETH-YES + BTC-NO

    agree = long.btc_result == long.eth_result
    long["payA"] = np.where(agree, 1.0, np.where(long.btc_result == "yes", 2.0, 0.0))
    long["payB"] = np.where(agree, 1.0, np.where(long.eth_result == "yes", 2.0, 0.0))
    return long.sort_values(["open_ts", "minute"]).reset_index(drop=True)


def load_single(series: str) -> pd.DataFrame:
    """Per-(window, minute) rows for one series: bid/ask/lo/hi closes + result."""
    con = sqlite3.connect(str(DB))
    mk = pd.read_sql_query(
        f"SELECT ticker, open_ts, close_ts, result FROM markets "
        f"WHERE series='{series}' AND result IN ('yes','no')", con)
    cd = pd.read_sql_query(
        "SELECT ticker, end_ts, bid_close AS bid, ask_close AS ask, "
        "price_low AS lo, price_high AS hi, volume FROM candles", con)
    con.close()
    cd = cd[cd.ticker.isin(set(mk.ticker))]
    df = mk.merge(cd, on="ticker", how="inner")
    df["minute"] = ((df.end_ts - df.open_ts) / 60).round().astype(int)
    df = df[(df.minute >= 1) & (df.minute <= 15)]
    return df.sort_values(["open_ts", "minute"]).reset_index(drop=True)


if __name__ == "__main__":
    df = load_windows()
    print(f"windows: {df.open_ts.nunique()}, rows: {len(df)}")
    print(df.tail(3).to_string())
