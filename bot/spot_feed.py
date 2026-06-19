"""Live spot feed from Coinbase with rolling realized vol.

Maintains, per asset: latest trade price, 1-min closes ring buffer for vol,
and per-window strike capture (avg of last 60s before window open).

Optionally persists 1-minute OHLC bars to a dedicated SQLite DB
(data/spot_history.db) so the live market data is retained for later research
(re-deriving volatility, calibrating/backtesting model tweaks). Storage is
bounded: one row per asset per minute (~7k rows/day for 5 assets) with
automatic retention pruning (SPOT_HISTORY_DAYS).
"""
import collections
import os
import sqlite3
import threading
import time
from pathlib import Path

import requests

PRODUCTS = {"btc": "BTC-USD", "eth": "ETH-USD", "sol": "SOL-USD",
            "xrp": "XRP-USD", "doge": "DOGE-USD"}
TICKER_URL = "https://api.exchange.coinbase.com/products/{}/ticker"

HISTORY_DB = Path(__file__).resolve().parents[1] / "data" / "spot_history.db"
HISTORY_DAYS = int(os.getenv("SPOT_HISTORY_DAYS", "180"))


class SpotFeed:
    """REST-polling spot feed (robust, ~1s freshness). Thread-safe reads.

    Set persist=True (the live engine does) to record 1-minute OHLC bars to
    data/spot_history.db. The read-only dashboard leaves persist=False so it
    never writes.
    """

    def __init__(self, assets, poll_seconds=1.0, vol_lookback_min=120,
                 persist=False):
        self.assets = list(assets)
        self.poll_seconds = poll_seconds
        self.vol_lookback = vol_lookback_min
        self.persist = persist
        self.last = {a: None for a in self.assets}          # latest price
        self.last_ts = {a: 0.0 for a in self.assets}
        # per-asset: deque of (minute_ts, close)
        self.minute_closes = {a: collections.deque(maxlen=vol_lookback_min + 5)
                              for a in self.assets}
        # per-asset finished 1-min OHLC bars awaiting persistence:
        #   deque of (minute_ts, open, high, low, close)
        self.minute_bars = {a: collections.deque(maxlen=1500)
                            for a in self.assets}
        # rolling 60s price samples for strike averaging: deque of (ts, price)
        self.samples = {a: collections.deque() for a in self.assets}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads = []

    def start(self):
        self._warm_start()
        for a in self.assets:
            t = threading.Thread(target=self._poll_loop, args=(a,), daemon=True)
            t.start()
            self._threads.append(t)
        if self.persist:
            t = threading.Thread(target=self._persist_loop, daemon=True)
            t.start()
            self._threads.append(t)

    def _warm_start(self):
        """Seed minute closes from Coinbase candles so sigma is available at once."""
        for a in self.assets:
            try:
                end = int(time.time() // 60) * 60
                start = end - self.vol_lookback * 60
                r = requests.get(
                    f"https://api.exchange.coinbase.com/products/{PRODUCTS[a]}/candles",
                    params={"granularity": 60,
                            "start": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(start)),
                            "end": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(end))},
                    timeout=10)
                bars = sorted(r.json(), key=lambda b: b[0])  # [ts, low, high, open, close, vol]
                with self._lock:
                    for b in bars:
                        self.minute_closes[a].append((b[0], float(b[4])))
            except Exception:
                pass

    def stop(self):
        self._stop.set()

    def _poll_loop(self, asset):
        sess = requests.Session()
        cur_min = None
        o = hi = lo = last_px = None     # current-minute OHLC accumulator
        vol = 0.0                        # summed size of distinct trades this min
        last_tid = None                  # dedupe trades across 1s polls
        while not self._stop.is_set():
            t0 = time.time()
            try:
                r = sess.get(TICKER_URL.format(PRODUCTS[asset]), timeout=5)
                if r.ok:
                    j = r.json()
                    px = float(j["price"])
                    tid = j.get("trade_id")
                    try:
                        sz = float(j.get("size") or 0.0)
                    except (TypeError, ValueError):
                        sz = 0.0
                    now = time.time()
                    with self._lock:
                        self.last[asset] = px
                        self.last_ts[asset] = now
                        self.samples[asset].append((now, px))
                        while self.samples[asset] and self.samples[asset][0][0] < now - 70:
                            self.samples[asset].popleft()
                        m = int(now // 60) * 60
                        if cur_min is None:
                            cur_min = m
                            o = hi = lo = last_px = px
                        if m > cur_min:
                            # minute rolled over: close out the prior bar
                            self.minute_closes[asset].append((cur_min, last_px))
                            self.minute_bars[asset].append(
                                (cur_min, o, hi, lo, last_px, vol))
                            cur_min = m
                            o = hi = lo = px
                            vol = 0.0
                        hi = px if hi is None else max(hi, px)
                        lo = px if lo is None else min(lo, px)
                        last_px = px
                        # new trade since last poll → count its size as volume
                        if tid is not None and tid != last_tid:
                            vol += sz
                            last_tid = tid
            except requests.RequestException:
                pass
            time.sleep(max(0.0, self.poll_seconds - (time.time() - t0)))

    def _persist_loop(self):
        """Write finished 1-min OHLC bars to data/spot_history.db, bounded by
        retention. Single writer thread → one connection, no cross-thread
        sharing. Robust to transient DB/lock errors (just retries next cycle)."""
        HISTORY_DB.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(HISTORY_DB), timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            "CREATE TABLE IF NOT EXISTS spot_1m ("
            "  asset TEXT NOT NULL, minute_ts INTEGER NOT NULL,"
            "  open REAL, high REAL, low REAL, close REAL, volume REAL,"
            "  PRIMARY KEY (asset, minute_ts))")
        # Migrate older DBs that predate the volume column.
        if "volume" not in {row[1] for row in con.execute(
                "PRAGMA table_info(spot_1m)")}:
            con.execute("ALTER TABLE spot_1m ADD COLUMN volume REAL")
        con.commit()
        last_prune = 0.0
        while not self._stop.is_set():
            time.sleep(30)
            try:
                with self._lock:
                    batch = [(a, *bar) for a in self.assets
                             for bar in self.minute_bars[a]]
                if batch:
                    con.executemany(
                        "INSERT OR IGNORE INTO spot_1m "
                        "(asset, minute_ts, open, high, low, close, volume) "
                        "VALUES (?,?,?,?,?,?,?)", batch)
                    con.commit()
                if time.time() - last_prune > 3600:
                    cutoff = int(time.time()) - HISTORY_DAYS * 86400
                    con.execute("DELETE FROM spot_1m WHERE minute_ts < ?", (cutoff,))
                    con.commit()
                    last_prune = time.time()
            except sqlite3.Error:
                pass
        con.close()

    def price(self, asset):
        with self._lock:
            if time.time() - self.last_ts[asset] > 10:
                return None
            return self.last[asset]

    def avg_60s(self, asset, end_ts=None):
        """Rolling average of samples in [end_ts-60, end_ts]."""
        end_ts = end_ts or time.time()
        with self._lock:
            pts = [p for ts, p in self.samples[asset] if end_ts - 60 <= ts <= end_ts]
        return sum(pts) / len(pts) if pts else None

    def sigma_1m(self, asset):
        """Std of 1-min log returns over the lookback. None if insufficient data."""
        with self._lock:
            closes = [c for _ts, c in self.minute_closes[asset]]
        return self._sigma_from_closes(closes, min_n=30)

    def sigma_1m_window(self, asset, minutes):
        """Std of 1-min log returns over only the most recent `minutes` closes.
        Used to detect short-horizon vol spikes vs the full-lookback baseline."""
        with self._lock:
            closes = [c for _ts, c in self.minute_closes[asset]][-(minutes + 1):]
        return self._sigma_from_closes(closes, min_n=8)

    @staticmethod
    def _sigma_from_closes(closes, min_n):
        import math
        if len(closes) < min_n:
            return None
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var)
