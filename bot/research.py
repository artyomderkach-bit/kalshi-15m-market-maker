"""Structured research data in data/spot_history.db.

High-value tables for later model retraining / execution tuning and backtests.
Writes are buffered and flushed periodically so the trading loop stays fast.

Tables:
  quote_decisions  — throttled eval log with book + model + skip/quote action
  window_timeline  — fixed checkpoints (min 1/5/7/10/13) for every window
  window_outcomes  — settle result + model view (extended columns)
  quote_lifecycle  — place / cancel / fill events with timing
  spot_ticks       — sub-minute spot during final 60s of traded windows only
  orderbook_snaps  — full Kalshi depth ladder (both sides) at checkpoints + a
                     fast ramp over the final 60s of windows we are working
  market_trades    — raw public trade prints (tape) for every active window

The order book and trade tape are the *raw* microstructure a future model
needs to re-derive fair value, queue position and fills under different quote
prices / latencies — things the 1-minute backtest candles cannot capture.
All high-volume tables are retention-bounded so the DB plateaus in size and
can run continuously for weeks without filling the disk.
"""
import datetime
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from .outcomes import TIMELINE_COLS
from .spot_feed import HISTORY_DB

DECISION_DAYS = int(os.getenv("DECISION_DAYS", "730"))
TIMELINE_DAYS = int(os.getenv("TIMELINE_DAYS", "730"))
LIFECYCLE_DAYS = int(os.getenv("LIFECYCLE_DAYS", "730"))
TICK_DAYS = int(os.getenv("SPOT_TICK_DAYS", "365"))
# High-volume raw microstructure: bounded tighter so weeks of continuous
# collection stays well under a few hundred MB even if left running.
ORDERBOOK_DAYS = int(os.getenv("ORDERBOOK_DAYS", "45"))
TRADES_DAYS = int(os.getenv("TRADES_DAYS", "45"))
ORDERBOOK_RAMP_SEC = float(os.getenv("ORDERBOOK_RAMP_SEC", "2"))
DECISION_INTERVAL_SEC = float(os.getenv("DECISION_LOG_INTERVAL_SEC", "60"))
TIMELINE_MINUTES = (1, 5, 7, 10, 13)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS quote_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    asset TEXT NOT NULL,
    open_ts INTEGER NOT NULL,
    minute REAL,
    ticker TEXT,
    strike REAL,
    spot REAL,
    sigma_long REAL,
    sigma_short REAL,
    vol_ratio REAL,
    margin_eff REAL,
    yes_bid REAL,
    yes_ask REAL,
    no_bid REAL,
    no_ask REAL,
    ls_side TEXT,
    ls_price REAL,
    model_fair REAL,
    prem REAL,
    fav_side TEXT,
    model_p_fav REAL,
    cost_fav REAL,
    action TEXT NOT NULL,
    skip_reason TEXT,
    kelly_qty INTEGER,
    order_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_qd_window ON quote_decisions(asset, open_ts);
CREATE INDEX IF NOT EXISTS idx_qd_ts ON quote_decisions(ts);

CREATE TABLE IF NOT EXISTS window_timeline (
    asset TEXT NOT NULL,
    open_ts INTEGER NOT NULL,
    minute INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    ticker TEXT,
    strike REAL,
    spot REAL,
    sigma_long REAL,
    sigma_short REAL,
    vol_ratio REAL,
    margin_eff REAL,
    yes_ask REAL,
    no_ask REAL,
    ls_side TEXT,
    ls_price REAL,
    prem REAL,
    model_p_fav REAL,
    secs_left REAL,
    PRIMARY KEY (asset, open_ts, minute)
);
CREATE INDEX IF NOT EXISTS idx_wt_open ON window_timeline(open_ts);

CREATE TABLE IF NOT EXISTS quote_lifecycle (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    asset TEXT NOT NULL,
    open_ts INTEGER NOT NULL,
    ticker TEXT,
    order_id TEXT,
    event TEXT NOT NULL,
    ls_side TEXT,
    price REAL,
    qty REAL,
    fair REAL,
    prem REAL,
    fill_qty REAL,
    seconds_in_book REAL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_ql_order ON quote_lifecycle(order_id);
CREATE INDEX IF NOT EXISTS idx_ql_window ON quote_lifecycle(asset, open_ts);

CREATE TABLE IF NOT EXISTS spot_ticks (
    asset TEXT NOT NULL,
    open_ts INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    spot REAL NOT NULL,
    secs_left REAL,
    PRIMARY KEY (asset, open_ts, ts)
);
CREATE INDEX IF NOT EXISTS idx_st_window ON spot_ticks(asset, open_ts);

CREATE TABLE IF NOT EXISTS orderbook_snaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    asset TEXT NOT NULL,
    open_ts INTEGER NOT NULL,
    ticker TEXT,
    minute REAL,
    secs_left REAL,
    phase TEXT,                  -- 'checkpoint' | 'ramp'
    spot REAL,
    best_yes_bid REAL,
    best_no_bid REAL,
    yes_ask REAL,
    no_ask REAL,
    yes_book TEXT,               -- JSON [[price,size],...] full yes-bid ladder
    no_book TEXT                 -- JSON [[price,size],...] full no-bid ladder
);
CREATE INDEX IF NOT EXISTS idx_obs_window ON orderbook_snaps(asset, open_ts);
CREATE INDEX IF NOT EXISTS idx_obs_ts ON orderbook_snaps(ts);

CREATE TABLE IF NOT EXISTS market_trades (
    asset TEXT NOT NULL,
    open_ts INTEGER NOT NULL,
    trade_id TEXT NOT NULL,
    ticker TEXT,
    ts INTEGER NOT NULL,
    yes_price REAL,
    no_price REAL,
    count REAL,
    taker_side TEXT,
    PRIMARY KEY (asset, open_ts, trade_id)
);
CREATE INDEX IF NOT EXISTS idx_mt_window ON market_trades(asset, open_ts);
CREATE INDEX IF NOT EXISTS idx_mt_ts ON market_trades(ts);
"""

_OUTCOME_COLS = (
    ("sigma_short", "REAL"),
    ("vol_ratio", "REAL"),
    ("margin_eff", "REAL"),
    ("prem_at_snap", "REAL"),
    ("yes_ask_at_snap", "REAL"),
    ("no_ask_at_snap", "REAL"),
    ("ls_side_at_snap", "TEXT"),
    ("ls_price_at_snap", "REAL"),
    ("floor_strike", "REAL"),
)


class ResearchLog:
    """Buffered writer to spot_history.db. Safe to call from the engine loop."""

    def __init__(self, db_path=None):
        self._db_path = Path(db_path) if db_path else HISTORY_DB
        self._con = None
        self._buf = []
        self._last_decision = {}   # (asset, open_ts) -> (ts, action, skip_reason)
        self._timeline_done = set()
        self._last_tick = {}       # (asset, open_ts) -> ts
        self._last_ob = {}         # (asset, open_ts) -> ts (ramp throttle)
        self._last_flush = 0.0
        self._last_prune = 0.0
        self._outcome_err_logged = False
        self._open()

    def _open(self):
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._con = sqlite3.connect(str(self._db_path), timeout=30)
            self._con.execute("PRAGMA journal_mode=WAL")
            self._con.execute("PRAGMA synchronous=NORMAL")
            self._con.executescript(_SCHEMA)
            self._migrate_outcomes()
            self._migrate_lifecycle()
            self._con.commit()
        except sqlite3.Error:
            self._con = None

    def _migrate_outcomes(self):
        cols = {r[1] for r in self._con.execute(
            "PRAGMA table_info(window_outcomes)")}
        if not cols:
            self._con.execute(
                "CREATE TABLE IF NOT EXISTS window_outcomes ("
                "  asset TEXT NOT NULL, open_ts INTEGER NOT NULL, ticker TEXT,"
                "  strike REAL, spot_ref REAL, sigma REAL, secs_left_ref REAL,"
                "  fav_side TEXT, model_p_fav REAL,"
                "  traded INTEGER DEFAULT 0, result TEXT, fav_won INTEGER,"
                "  recorded_ts INTEGER,"
                "  PRIMARY KEY (asset, open_ts))")
            cols = set()
        for name, typ in _OUTCOME_COLS:
            if name not in cols:
                self._con.execute(
                    f"ALTER TABLE window_outcomes ADD COLUMN {name} {typ}")

    def _migrate_lifecycle(self):
        """Add the A/B arm tag to quote_lifecycle (cancel-race harness)."""
        cols = {r[1] for r in self._con.execute(
            "PRAGMA table_info(quote_lifecycle)")}
        if cols and "arm" not in cols:
            self._con.execute("ALTER TABLE quote_lifecycle ADD COLUMN arm TEXT")

    @property
    def ok(self):
        return self._con is not None

    def _queue(self, sql, row):
        if self._con is None:
            return
        self._buf.append((sql, row))
        if len(self._buf) >= 40:
            self.flush()

    def flush(self):
        if self._con is None or not self._buf:
            return
        try:
            for sql, row in self._buf:
                self._con.execute(sql, row)
            self._con.commit()
            self._buf.clear()
            self._last_flush = time.time()
        except sqlite3.Error:
            self._buf.clear()

    def maybe_flush(self):
        if time.time() - self._last_flush > 30:
            self.flush()

    def log_decision(self, row: dict):
        """Throttled: always log quote/cancel/fill; skip reasons at most every
        DECISION_INTERVAL_SEC unless the reason changes."""
        key = (row["asset"], row["open_ts"])
        action = row["action"]
        reason = row.get("skip_reason")
        prev = self._last_decision.get(key)
        now = row["ts"]
        if action not in ("quote", "cancel", "fill", "partial_fill", "placed"):
            prev = self._last_decision.get(key)
            if prev and now - prev[0] < DECISION_INTERVAL_SEC:
                if prev[1] == action and prev[2] == reason:
                    return
        self._last_decision[key] = (now, action, reason)
        self._queue(
            "INSERT INTO quote_decisions "
            "(ts,asset,open_ts,minute,ticker,strike,spot,sigma_long,sigma_short,"
            " vol_ratio,margin_eff,yes_bid,yes_ask,no_bid,no_ask,ls_side,ls_price,"
            " model_fair,prem,fav_side,model_p_fav,cost_fav,action,skip_reason,"
            " kelly_qty,order_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "?,?,?,?,?,?)",
            (
                row["ts"], row["asset"], row["open_ts"], row.get("minute"),
                row.get("ticker"), row.get("strike"), row.get("spot"),
                row.get("sigma_long"), row.get("sigma_short"), row.get("vol_ratio"),
                row.get("margin_eff"), row.get("yes_bid"), row.get("yes_ask"),
                row.get("no_bid"), row.get("no_ask"), row.get("ls_side"),
                row.get("ls_price"), row.get("model_fair"), row.get("prem"),
                row.get("fav_side"), row.get("model_p_fav"), row.get("cost_fav"),
                action, reason, row.get("kelly_qty"), row.get("order_id"),
            ))

    def log_timeline(self, row: dict):
        key = (row["asset"], row["open_ts"], row["minute"])
        if key in self._timeline_done:
            return
        self._timeline_done.add(key)
        self._queue(
            "INSERT OR REPLACE INTO window_timeline "
            "(asset,open_ts,minute,ts,ticker,strike,spot,sigma_long,sigma_short,"
            " vol_ratio,margin_eff,yes_ask,no_ask,ls_side,ls_price,prem,"
            " model_p_fav,secs_left) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["asset"], row["open_ts"], row["minute"], row["ts"],
                row.get("ticker"), row.get("strike"), row.get("spot"),
                row.get("sigma_long"), row.get("sigma_short"), row.get("vol_ratio"),
                row.get("margin_eff"), row.get("yes_ask"), row.get("no_ask"),
                row.get("ls_side"), row.get("ls_price"), row.get("prem"),
                row.get("model_p_fav"), row.get("secs_left"),
            ))

    def log_lifecycle(self, row: dict):
        self._queue(
            "INSERT INTO quote_lifecycle "
            "(ts,asset,open_ts,ticker,order_id,event,ls_side,price,qty,fair,prem,"
            " fill_qty,seconds_in_book,note,arm) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["ts"], row["asset"], row["open_ts"], row.get("ticker"),
                row.get("order_id"), row["event"], row.get("ls_side"),
                row.get("price"), row.get("qty"), row.get("fair"), row.get("prem"),
                row.get("fill_qty"), row.get("seconds_in_book"), row.get("note"),
                row.get("arm"),
            ))

    def log_spot_tick(self, asset, open_ts, spot, secs_left):
        now = int(time.time())
        key = (asset, open_ts)
        last = self._last_tick.get(key, 0)
        if now - last < 5:
            return
        self._last_tick[key] = now
        self._queue(
            "INSERT OR IGNORE INTO spot_ticks (asset,open_ts,ts,spot,secs_left) "
            "VALUES (?,?,?,?,?)",
            (asset, open_ts, now, spot, secs_left))

    def log_orderbook(self, row: dict, phase: str):
        """Persist the full depth ladder (both sides) from an engine evaluation.
        Checkpoints are always stored; ramp snapshots are throttled to at most
        once per ORDERBOOK_RAMP_SEC per window so a fast poll loop can't blow up
        the table. Skips silently when the row has no book (e.g. early skips)."""
        if self._con is None:
            return
        yes_book = row.get("yes_book")
        no_book = row.get("no_book")
        if not yes_book and not no_book:
            return
        if phase == "ramp":
            key = (row["asset"], row["open_ts"])
            now = time.time()
            if now - self._last_ob.get(key, 0.0) < ORDERBOOK_RAMP_SEC:
                return
            self._last_ob[key] = now
        self._queue(
            "INSERT INTO orderbook_snaps "
            "(ts,asset,open_ts,ticker,minute,secs_left,phase,spot,best_yes_bid,"
            " best_no_bid,yes_ask,no_ask,yes_book,no_book) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row.get("ts", int(time.time())), row["asset"], row["open_ts"],
                row.get("ticker"), row.get("minute"), row.get("secs_left"), phase,
                row.get("spot"), row.get("yes_bid"), row.get("no_bid"),
                row.get("yes_ask"), row.get("no_ask"),
                json.dumps(yes_book) if yes_book else None,
                json.dumps(no_book) if no_book else None,
            ))

    def log_trades(self, asset, open_ts, ticker, trades):
        """Append raw public trade prints (deduped by trade_id). Returns the max
        created-time seen so the caller can advance its fetch cursor."""
        if self._con is None or not trades:
            return None
        max_ts = None
        for t in trades:
            ypd = t.get("yes_price_dollars")
            yp = (float(ypd) if ypd is not None
                  else float(t.get("yes_price") or 0) / 100.0)
            npd = t.get("no_price_dollars")
            npp = (float(npd) if npd is not None
                   else float(t.get("no_price") or 0) / 100.0)
            cts = None
            created = t.get("created_time", "")
            if created:
                try:
                    cts = int(datetime.datetime.fromisoformat(
                        created.replace("Z", "+00:00")).timestamp())
                except ValueError:
                    cts = None
            if cts is None:
                cts = int(t.get("ts") or time.time())
            tid = str(t.get("trade_id") or t.get("id")
                      or f"{cts}-{yp:.4f}-{t.get('count')}")
            count = t.get("count")
            taker = t.get("taker_side")
            self._queue(
                "INSERT OR IGNORE INTO market_trades "
                "(asset,open_ts,trade_id,ticker,ts,yes_price,no_price,count,"
                " taker_side) VALUES (?,?,?,?,?,?,?,?,?)",
                (asset, open_ts, tid, ticker, cts, yp, npp,
                 float(count) if count is not None else None, taker))
            max_ts = cts if max_ts is None else max(max_ts, cts)
        return max_ts

    def record_outcome(self, row: dict):
        if self._con is None:
            return
        try:
            self.flush()
            self._con.execute(
                "INSERT OR REPLACE INTO window_outcomes "
                "(asset, open_ts, ticker, strike, spot_ref, sigma, secs_left_ref,"
                " fav_side, model_p_fav, traded, result, fav_won, recorded_ts,"
                " sigma_short, vol_ratio, margin_eff, prem_at_snap,"
                " yes_ask_at_snap, no_ask_at_snap, ls_side_at_snap,"
                " ls_price_at_snap, floor_strike) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",  # 22 cols
                (
                    row["asset"], row["open_ts"], row.get("ticker"),
                    row.get("strike"), row.get("spot_ref"), row.get("sigma"),
                    row.get("secs_left_ref"), row.get("fav_side"),
                    row.get("model_p_fav"), row.get("traded", 0),
                    row.get("result"), row.get("fav_won"), row.get("recorded_ts"),
                    row.get("sigma_short"), row.get("vol_ratio"),
                    row.get("margin_eff"), row.get("prem_at_snap"),
                    row.get("yes_ask_at_snap"), row.get("no_ask_at_snap"),
                    row.get("ls_side_at_snap"), row.get("ls_price_at_snap"),
                    row.get("floor_strike"),
                ))
            self._con.commit()
        except sqlite3.Error as e:
            # Surface the first failure (to stderr -> bot.log). A silently
            # swallowed binding mismatch here is exactly what kept the
            # supervised label empty for weeks.
            if not self._outcome_err_logged:
                self._outcome_err_logged = True
                sys.stderr.write(f"[research] window_outcomes write failed: {e}\n")

    def windows_needing_outcomes(self, min_open_ts, max_open_ts, limit=200):
        """``[(asset, open_ts)]`` that have a timeline snapshot but no label yet,
        for closed windows in ``[min_open_ts, max_open_ts)``. Drives the engine
        sweep and the backfill so the label no longer depends on in-memory snaps."""
        if self._con is None:
            return []
        self.flush()
        try:
            rows = self._con.execute(
                "SELECT DISTINCT t.asset, t.open_ts FROM window_timeline t "
                "LEFT JOIN window_outcomes o "
                "  ON o.asset = t.asset AND o.open_ts = t.open_ts "
                "WHERE o.open_ts IS NULL AND t.open_ts >= ? AND t.open_ts < ? "
                "ORDER BY t.open_ts LIMIT ?",
                (int(min_open_ts), int(max_open_ts), int(limit))).fetchall()
        except sqlite3.Error:
            return []
        return [(r[0], r[1]) for r in rows]

    def timeline_snapshot(self, asset, open_ts):
        """Best persisted snapshot for a window as a dict (keys = TIMELINE_COLS):
        the row nearest minute 7 that still carries a longshot side, so the
        favorite is always recoverable. ``None`` if the window has no timeline."""
        if self._con is None:
            return None
        try:
            r = self._con.execute(
                "SELECT minute, ticker, strike, spot, sigma_long, sigma_short, "
                "       vol_ratio, margin_eff, yes_ask, no_ask, ls_side, ls_price, "
                "       prem, model_p_fav, secs_left "
                "FROM window_timeline WHERE asset = ? AND open_ts = ? "
                "ORDER BY (ls_side IS NULL), ABS(minute - 7), minute LIMIT 1",
                (asset, int(open_ts))).fetchone()
        except sqlite3.Error:
            return None
        return dict(zip(TIMELINE_COLS, r)) if r else None

    def prune_memory(self, open_ts_cutoff):
        for k in [k for k in self._last_decision if k[1] < open_ts_cutoff]:
            self._last_decision.pop(k, None)
        for k in [k for k in self._timeline_done if k[1] < open_ts_cutoff]:
            self._timeline_done.discard(k)
        for k in [k for k in self._last_tick if k[1] < open_ts_cutoff]:
            self._last_tick.pop(k, None)
        for k in [k for k in self._last_ob if k[1] < open_ts_cutoff]:
            self._last_ob.pop(k, None)

    def prune_db(self):
        if self._con is None or time.time() - self._last_prune < 3600:
            return
        now = int(time.time())
        try:
            self.flush()
            self._con.execute(
                "DELETE FROM quote_decisions WHERE ts < ?",
                (now - DECISION_DAYS * 86400,))
            self._con.execute(
                "DELETE FROM window_timeline WHERE open_ts < ?",
                (now - TIMELINE_DAYS * 86400,))
            self._con.execute(
                "DELETE FROM quote_lifecycle WHERE ts < ?",
                (now - LIFECYCLE_DAYS * 86400,))
            self._con.execute(
                "DELETE FROM spot_ticks WHERE ts < ?",
                (now - TICK_DAYS * 86400,))
            self._con.execute(
                "DELETE FROM window_outcomes WHERE open_ts < ?",
                (now - int(os.getenv("WINDOW_OUTCOME_DAYS", "730")) * 86400,))
            self._con.execute(
                "DELETE FROM orderbook_snaps WHERE ts < ?",
                (now - ORDERBOOK_DAYS * 86400,))
            self._con.execute(
                "DELETE FROM market_trades WHERE ts < ?",
                (now - TRADES_DAYS * 86400,))
            self._con.commit()
            # Keep the WAL file from growing without bound over weeks of running.
            self._con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._last_prune = time.time()
        except sqlite3.Error:
            pass
