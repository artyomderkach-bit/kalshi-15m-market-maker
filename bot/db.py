"""SQLite ledger for the trading bot. Bot writes; dashboard reads read-only."""
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "bot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    mode TEXT NOT NULL,              -- paper | live
    window_open_ts INTEGER,
    ticker TEXT NOT NULL,
    asset TEXT,                      -- btc | eth
    side TEXT NOT NULL,              -- yes | no
    action TEXT NOT NULL,            -- buy | sell | settle
    qty REAL NOT NULL,
    price REAL NOT NULL,             -- dollars per contract
    fees REAL NOT NULL DEFAULT 0,
    order_id TEXT,
    is_maker INTEGER DEFAULT 0,
    model_p REAL,                    -- model fair prob at decision time
    edge REAL,                       -- model edge at decision time
    note TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    ticker TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    window_open_ts INTEGER,
    asset TEXT,
    side TEXT,
    qty REAL NOT NULL,
    avg_cost REAL NOT NULL,
    fees REAL NOT NULL DEFAULT 0,
    settled INTEGER DEFAULT 0,
    result TEXT,
    pnl REAL
);
CREATE TABLE IF NOT EXISTS equity (
    ts INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    balance REAL,                    -- live: API balance; paper: simulated
    open_risk REAL,
    realized_pnl REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    level TEXT,
    msg TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_trades_window ON trades(window_open_ts);
"""


def connect(readonly=False) -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    if readonly:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    else:
        con = sqlite3.connect(str(DB_PATH), timeout=30)
        con.executescript(SCHEMA)
    con.row_factory = sqlite3.Row
    return con


def log_event(con, level, msg):
    con.execute("INSERT INTO events (ts, level, msg) VALUES (?,?,?)",
                (int(time.time()), level, msg))
    con.commit()


def record_trade(con, **kw):
    cols = ["ts", "mode", "window_open_ts", "ticker", "asset", "side", "action",
            "qty", "price", "fees", "order_id", "is_maker", "model_p", "edge", "note"]
    kw.setdefault("ts", int(time.time()))
    vals = [kw.get(c) for c in cols]
    con.execute(f"INSERT INTO trades ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})", vals)
    con.commit()
