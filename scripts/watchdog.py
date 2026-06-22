"""Autonomous health watchdog for the live bot.

Run by cron every 2h, 24/7. Each run:
  - verifies systemd services are active; restarts any that are down
  - checks heartbeat freshness; restarts the bot if stale (hung)
  - snapshots performance + system state to health.log
  - removes the daily summary file once it is older than 24 hours

Daily summary (9:30 AM Central, overwritten each morning, expires after 24h):
  30 9 * * * ... python scripts/watchdog.py summary

Designed to need no human and no external session.

Cron (on the server):
  0 */2 * * * cd /home/ubuntu/kalshi-15m-bot && .venv/bin/python3 scripts/watchdog.py # kalshi-watchdog
  CRON_TZ=America/Chicago
  30 9 * * * cd /home/ubuntu/kalshi-15m-bot && .venv/bin/python3 scripts/watchdog.py summary # kalshi-daily-summary
"""
import datetime
import os
import re
import subprocess
import sqlite3
import sys
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Load .env so cron-invoked runs see SMTP_* / EMAIL_* (cron has a bare env).
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except Exception:
    pass

from bot.emailer import send_email

DB = os.path.join(ROOT, "data", "bot.db")
LOG = os.path.join(ROOT, "health.log")
SUMMARY = os.path.join(ROOT, "data", "daily_summary.txt")
# Live-vs-backtest comparison block, pushed from the Mac by
# backtest/rolling_live_vs_backtest.py (runs 9:00 Central, before this 9:30 email).
COMPARISON = os.path.join(ROOT, "data", "live_vs_backtest_summary.txt")
COMPARISON_STALE_SEC = 36 * 3600
HEARTBEAT_STALE_MIN = 12
SUMMARY_TTL_SEC = 86400
CENTRAL = ZoneInfo("America/Chicago")
EMAIL_SUBJECT = "Kalshi 15m Crypto Bot — Daily Summary"


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


def svc_active(name):
    return sh(f"systemctl is-active {name}") == "active"


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def central_now():
    return now().astimezone(CENTRAL)


def log(lines):
    stamp = now().strftime("%Y-%m-%d %H:%M:%S UTC")
    with open(LOG, "a") as f:
        f.write(f"\n===== {stamp} =====\n")
        for ln in lines:
            f.write(ln + "\n")


def expire_stale_summary():
    try:
        age = now().timestamp() - os.path.getmtime(SUMMARY)
        if age > SUMMARY_TTL_SEC:
            os.remove(SUMMARY)
    except FileNotFoundError:
        pass


def read_trade_stats(since_ts=None):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    rows = con.execute(
        "SELECT action, note, ts FROM trades WHERE mode='live'").fetchall()
    hb_ts = con.execute(
        "SELECT MAX(ts) FROM events WHERE msg LIKE 'heartbeat%'").fetchone()[0]
    err_since = since_ts if since_ts is not None else int(now().timestamp()) - 7200
    err_n = con.execute(
        "SELECT COUNT(*) FROM events WHERE level='error' AND ts >= ?",
        (err_since,)).fetchone()[0]
    buy_keys = {
        (r[0], r[1]) for r in con.execute(
            "SELECT ticker, window_open_ts FROM trades "
            "WHERE mode='live' AND action='buy'"
        ).fetchall()
    }
    settle_keys = {
        (r[0], r[1]) for r in con.execute(
            "SELECT ticker, window_open_ts FROM trades "
            "WHERE mode='live' AND action='settle'"
        ).fetchall()
    }
    con.close()

    settles = [r for r in rows if r[0] == "settle"]
    buys = [r for r in rows if r[0] == "buy"]
    pnls = [float(r[1].split("=")[1]) for r in settles if r[1] and "pnl=" in r[1]]

    window = settles
    if since_ts is not None:
        window = [r for r in settles if r[2] >= since_ts]
    w_pnls = [float(r[1].split("=")[1]) for r in window if r[1] and "pnl=" in r[1]]

    hb_age = None
    if hb_ts:
        hb_age = (now().timestamp() - hb_ts) / 60.0

    return {
        "hb_age": hb_age,
        "err_n": err_n,
        "open_pos": len(buy_keys - settle_keys),
        "fills": len(buys),
        "all": {
            "settles": len(settles),
            "pnl": sum(pnls),
            "wins": sum(1 for p in pnls if p > 0),
            "losses": sum(1 for p in pnls if p <= 0),
        },
        "window": {
            "settles": len(window),
            "pnl": sum(w_pnls),
            "wins": sum(1 for p in w_pnls if p > 0),
            "losses": sum(1 for p in w_pnls if p <= 0),
        },
        "last_2h": _pnl_since(rows, now().timestamp() - 7200),
    }


def _pnl_since(rows, since_ts):
    settles = [r for r in rows if r[0] == "settle" and r[2] > since_ts]
    pnls = [float(r[1].split("=")[1]) for r in settles if r[1] and "pnl=" in r[1]]
    return len(pnls), sum(pnls)


def watchdog_actions_since(since_ts):
    if not os.path.isfile(LOG):
        return []
    actions = []
    block_ts = None
    with open(LOG) as f:
        for line in f:
            m = re.match(r"===== (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC =====", line)
            if m:
                block_ts = datetime.datetime.strptime(
                    m.group(1), "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=datetime.timezone.utc).timestamp()
                continue
            if block_ts is None or block_ts < since_ts:
                continue
            if line.startswith("ACTIONS: ") and "none (healthy)" not in line:
                actions.append(line.strip()[len("ACTIONS: "):])
    return actions


def read_comparison_block():
    """The live-vs-backtest summary pushed from the Mac, or None. Adds a staleness
    note if the Mac didn't refresh it recently (e.g. the laptop was asleep)."""
    try:
        age = now().timestamp() - os.path.getmtime(COMPARISON)
        with open(COMPARISON) as f:
            txt = f.read().strip()
    except FileNotFoundError:
        return None
    if not txt:
        return None
    if age > COMPARISON_STALE_SEC:
        txt += f"\n  (stale: last updated {age/3600:.0f}h ago)"
    return txt


def write_daily_summary():
    os.makedirs(os.path.dirname(SUMMARY), exist_ok=True)
    since = now().timestamp() - SUMMARY_TTL_SEC
    stats = read_trade_stats(since_ts=since)
    actions = watchdog_actions_since(since)
    generated = central_now()
    expires = generated + datetime.timedelta(seconds=SUMMARY_TTL_SEC)

    bot = "active" if svc_active("kalshi-15m-bot") else "DOWN"
    dash = "active" if svc_active("kalshi-15m-dash") else "DOWN"
    hb = f"{stats['hb_age']:.1f} min ago" if stats["hb_age"] is not None else "unknown"
    mem = sh("free -m | awk 'NR==2{print $3\"/\"$2\"MB used, \"$7\"MB avail\"}'")
    w = stats["window"]
    a = stats["all"]

    lines = [
        "Daily bot summary",
        f"Generated: {generated:%Y-%m-%d %I:%M %p} Central",
        f"Expires:   {expires:%Y-%m-%d %I:%M %p} Central",
        "",
        f"Services:  bot={bot}, dash={dash}",
        f"Heartbeat: {hb}",
        f"Memory:    {mem}",
        "",
        "Last 24 hours:",
        f"  Settles: {w['settles']}   PnL: ${w['pnl']:+.2f}   "
        f"Wins: {w['wins']}   Losses: {w['losses']}",
        f"  Errors logged: {stats['err_n']}",
    ]
    if actions:
        lines.append(f"  Watchdog actions ({len(actions)}):")
        for act in actions[-5:]:
            lines.append(f"    - {act}")
    else:
        lines.append("  Watchdog actions: none")

    lines.extend([
        "",
        "All-time live:",
        f"  Settles: {a['settles']}   Total PnL: ${a['pnl']:+.2f}   "
        f"Wins: {a['wins']}   Losses: {a['losses']}   Open: {stats['open_pos']}",
    ])

    comparison = read_comparison_block()
    if comparison:
        lines.extend(["", "-" * 40, comparison])

    body = "\n".join(lines) + "\n"
    with open(SUMMARY, "w") as f:
        f.write(body)

    status = send_email(f"{EMAIL_SUBJECT} — {central_now():%b %d}", body)
    log([f"daily summary written; {status}"])


def main():
    expire_stale_summary()
    out = []
    actions = []

    # 1. service health + self-heal
    for svc in ("kalshi-15m-bot", "kalshi-15m-dash"):
        if not svc_active(svc):
            subprocess.run(f"sudo systemctl restart {svc}", shell=True)
            actions.append(f"RESTARTED {svc} (was down)")
        out.append(f"{svc}: {'active' if svc_active(svc) else 'DOWN'}")

    # 2. heartbeat freshness + perf snapshot
    try:
        stats = read_trade_stats()
        hb_age = stats["hb_age"]
        if hb_age is not None:
            out.append(f"last heartbeat: {hb_age:.1f} min ago")
        a = stats["all"]
        n2, p2 = stats["last_2h"]
        out.append(f"LIVE: fills={stats['fills']} settled={a['settles']} "
                   f"open={stats['open_pos']} totalPnL=${a['pnl']:.2f} "
                   f"wins={a['wins']} losses={a['losses']}")
        out.append(f"last 2h: settles={n2} pnl=${p2:.2f}")
    except Exception as e:
        hb_age = None
        out.append(f"DB read error: {e}")

    if hb_age is not None and hb_age > HEARTBEAT_STALE_MIN and svc_active("kalshi-15m-bot"):
        subprocess.run("sudo systemctl restart kalshi-15m-bot", shell=True)
        actions.append(f"RESTARTED bot (heartbeat stale {hb_age:.0f} min)")

    # 3. system
    mem = sh("free -m | awk 'NR==2{print $3\"/\"$2\"MB used, \"$7\"MB avail\"}'")
    out.append(f"mem: {mem}")

    if actions:
        out.append("ACTIONS: " + "; ".join(actions))
    else:
        out.append("ACTIONS: none (healthy)")

    log(out)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "summary":
        write_daily_summary()
    else:
        main()
