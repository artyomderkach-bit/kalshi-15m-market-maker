"""Bulk-fetch settled KXBTC15M/KXETH15M markets + 1-min candlesticks into data/market_data.db.

Resumable: markets already having candles are skipped.
"""
import datetime
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.kalshi_client import KalshiClient  # noqa: E402

DB = Path(__file__).resolve().parents[1] / "data" / "market_data.db"
SERIES = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M"]
START_TS = int(datetime.datetime(2025, 12, 10, tzinfo=datetime.timezone.utc).timestamp())

db_lock = threading.Lock()


def iso_to_ts(s):
    return int(datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def init_db():
    con = sqlite3.connect(str(DB), check_same_thread=False)
    con.execute("""CREATE TABLE IF NOT EXISTS markets (
        ticker TEXT PRIMARY KEY, series TEXT, open_ts INTEGER, close_ts INTEGER, result TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS candles (
        ticker TEXT, end_ts INTEGER,
        bid_open REAL, bid_high REAL, bid_low REAL, bid_close REAL,
        ask_open REAL, ask_high REAL, ask_low REAL, ask_close REAL,
        price_open REAL, price_high REAL, price_low REAL, price_close REAL,
        volume REAL, open_interest REAL,
        PRIMARY KEY (ticker, end_ts))""")
    con.execute("CREATE TABLE IF NOT EXISTS fetched (ticker TEXT PRIMARY KEY)")
    con.commit()
    return con


def fetch_market_list(client, con):
    for series in SERIES:
        cursor = None
        total = 0
        while True:
            r = client.get_markets(series, status="settled", cursor=cursor,
                                   limit=1000, min_close_ts=START_TS)
            mkts = r.get("markets", [])
            rows = [(m["ticker"], series, iso_to_ts(m["open_time"]),
                     iso_to_ts(m["close_time"]), m.get("result", "")) for m in mkts]
            with db_lock:
                con.executemany("INSERT OR REPLACE INTO markets VALUES (?,?,?,?,?)", rows)
                con.commit()
            total += len(rows)
            cursor = r.get("cursor")
            if not cursor or not mkts:
                break
        print(f"{series}: {total} settled markets", flush=True)


def f(d, *keys):
    for k in keys:
        d = d.get(k) if isinstance(d, dict) else None
        if d is None:
            return None
    try:
        return float(d)
    except (TypeError, ValueError):
        return None


def fetch_candles_for(client, con, ticker, series, open_ts, close_ts):
    cs = client.get_candlesticks(series, ticker, open_ts, close_ts, 1)["candlesticks"]
    rows = []
    for c in cs:
        rows.append((
            ticker, c["end_period_ts"],
            f(c, "yes_bid", "open_dollars"), f(c, "yes_bid", "high_dollars"),
            f(c, "yes_bid", "low_dollars"), f(c, "yes_bid", "close_dollars"),
            f(c, "yes_ask", "open_dollars"), f(c, "yes_ask", "high_dollars"),
            f(c, "yes_ask", "low_dollars"), f(c, "yes_ask", "close_dollars"),
            f(c, "price", "open_dollars"), f(c, "price", "high_dollars"),
            f(c, "price", "low_dollars"), f(c, "price", "close_dollars"),
            f(c, "volume_fp") or 0.0, f(c, "open_interest_fp") or 0.0,
        ))
    with db_lock:
        con.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.execute("INSERT OR REPLACE INTO fetched VALUES (?)", (ticker,))
        con.commit()


def main():
    con = init_db()
    client = KalshiClient(min_interval=0.15)
    if "--skip-list" not in sys.argv:
        fetch_market_list(client, con)

    todo = con.execute("""SELECT m.ticker, m.series, m.open_ts, m.close_ts FROM markets m
                          LEFT JOIN fetched ft ON ft.ticker = m.ticker
                          WHERE ft.ticker IS NULL ORDER BY m.open_ts""").fetchall()
    print(f"need candles for {len(todo)} markets", flush=True)

    done = 0
    errs = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(fetch_candles_for, client, con, t, s, o, c): t
                for t, s, o, c in todo}
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception as e:
                errs += 1
                if errs < 20:
                    print(f"ERR {futs[fut]}: {e}", flush=True)
            done += 1
            if done % 500 == 0:
                rate = done / (time.time() - t0)
                eta_min = (len(todo) - done) / rate / 60 if rate > 0 else 0
                print(f"{done}/{len(todo)} ({rate:.1f}/s, eta {eta_min:.0f}m, errs={errs})", flush=True)
    print(f"DONE: {done} fetched, {errs} errors", flush=True)


if __name__ == "__main__":
    main()
