"""Fetch 1-min spot candles from Coinbase Exchange into data/spot.db.

Coinbase is used as the reference spot series for realized-vol estimation and
backtesting.
"""
import datetime
import sqlite3
import time
from pathlib import Path

import requests

DB = Path(__file__).resolve().parents[1] / "data" / "spot.db"
PRODUCTS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD",
            "XRP": "XRP-USD", "DOGE": "DOGE-USD"}
START = datetime.datetime(2025, 12, 10, tzinfo=datetime.timezone.utc)
URL = "https://api.exchange.coinbase.com/products/{}/candles"
CHUNK = 300  # max bars per request


def main():
    con = sqlite3.connect(str(DB))
    con.execute("""CREATE TABLE IF NOT EXISTS spot (
        symbol TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY (symbol, ts))""")
    now = datetime.datetime.now(datetime.timezone.utc)
    for sym, product in PRODUCTS.items():
        row = con.execute("SELECT MAX(ts) FROM spot WHERE symbol=?", (sym,)).fetchone()
        cur = (datetime.datetime.fromtimestamp(row[0] + 60, datetime.timezone.utc)
               if row and row[0] else START)
        n = 0
        sess = requests.Session()
        while cur < now:
            end = cur + datetime.timedelta(minutes=CHUNK)
            for attempt in range(5):
                r = sess.get(URL.format(product),
                             params={"granularity": 60,
                                     "start": cur.isoformat(),
                                     "end": end.isoformat()},
                             timeout=15)
                if r.status_code == 429:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                r.raise_for_status()
                break
            bars = r.json()
            if bars:
                con.executemany("INSERT OR REPLACE INTO spot VALUES (?,?,?,?,?,?,?)",
                                [(sym, b[0], b[3], b[2], b[1], b[4], b[5]) for b in bars])
                con.commit()
                n += len(bars)
            cur = end
            if n and n % 30000 < CHUNK:
                print(f"{sym}: {n} bars, at {cur}", flush=True)
            time.sleep(0.12)
        print(f"{sym}: total {n} bars", flush=True)
    con.close()


if __name__ == "__main__":
    main()
