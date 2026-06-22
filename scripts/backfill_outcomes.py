"""One-time backfill for window_outcomes (the supervised label).

The label stopped being written after the first window because the engine
recorded it only from an in-memory dict that is not rehydrated on restart. The
features (window_timeline) and results (our ledger / the exchange) are all still
on disk, so every missing label is recoverable. This rebuilds them.

For each window that has a window_timeline snapshot but no window_outcomes row:
  * snapshot  <- window_timeline (row nearest minute 7 that has a longshot side)
  * result    <- our own ledger (bot.db) for windows we traded, no API call;
                 the exchange (get_market) for untraded windows, only with --api
  * label     <- bot.outcomes.build_outcome_row  (same code the live engine uses)

Safe to re-run (INSERT OR REPLACE, idempotent). Defaults to a dry run.

    python scripts/backfill_outcomes.py                 # dry run, ledger only
    python scripts/backfill_outcomes.py --api            # dry run, + exchange
    python scripts/backfill_outcomes.py --api --commit   # write for real
    python scripts/backfill_outcomes.py --history <copy.db> --ledger <bot.db> --commit
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config                       # noqa: E402
from bot.outcomes import build_outcome_row, fav_side_from  # noqa: E402
from bot.research import ResearchLog          # noqa: E402
from bot.spot_feed import HISTORY_DB          # noqa: E402

WINDOW_SEC = 900


def ledger_result(led, mode, asset, open_ts):
    """(result, traded) from the bot ledger. Mirrors engine._ledger_result."""
    buy = led.execute(
        "SELECT side FROM trades WHERE mode=? AND action='buy' "
        "AND asset=? AND window_open_ts=? LIMIT 1",
        (mode, asset, int(open_ts))).fetchone()
    if not buy:
        return None, False
    fav = buy["side"]
    st = led.execute(
        "SELECT price FROM trades WHERE mode=? AND action='settle' "
        "AND asset=? AND window_open_ts=? LIMIT 1",
        (mode, asset, int(open_ts))).fetchone()
    if not st:
        return None, True
    won = st["price"] >= 0.5
    return (fav if won else ("no" if fav == "yes" else "yes")), True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", default=str(HISTORY_DB), help="spot_history.db")
    ap.add_argument("--ledger", default=str(config.PROJECT_ROOT / "data" / "bot.db"))
    ap.add_argument("--mode", default=("paper" if config.PAPER_MODE else "live"))
    ap.add_argument("--api", action="store_true",
                    help="use the exchange to resolve untraded windows")
    ap.add_argument("--commit", action="store_true",
                    help="write rows (default is a dry run)")
    ap.add_argument("--limit", type=int, default=1_000_000)
    args = ap.parse_args()

    rl = ResearchLog(db_path=args.history)
    if not rl.ok:
        print(f"cannot open history db: {args.history}")
        return 1
    led = sqlite3.connect(f"file:{args.ledger}?mode=ro", uri=True, timeout=30)
    led.row_factory = sqlite3.Row

    client = None
    if args.api:
        from bot.kalshi_client import KalshiClient
        client = KalshiClient(min_interval=0.10)

    now = int(time.time())
    hi = now - WINDOW_SEC - 20                # only closed windows
    pending = rl.windows_needing_outcomes(0, hi, limit=args.limit)
    print(f"mode={args.mode}  pending windows without a label: {len(pending)}  "
          f"({'COMMIT' if args.commit else 'dry-run'}, api={'on' if args.api else 'off'})")

    n_written = n_ledger = n_api = n_no_result = n_no_snap = n_bad = 0
    fav_agree = fav_total = 0
    wins = 0
    for asset, open_ts in pending:
        snap = rl.timeline_snapshot(asset, open_ts)
        if not snap:
            n_no_snap += 1
            continue
        result, traded = ledger_result(led, args.mode, asset, open_ts)
        if traded and result is not None:
            n_ledger += 1
            # cross-check: timeline-derived favorite vs the side we actually bought
            buy = led.execute(
                "SELECT side FROM trades WHERE mode=? AND action='buy' "
                "AND asset=? AND window_open_ts=? LIMIT 1",
                (args.mode, asset, int(open_ts))).fetchone()
            tl_fav = fav_side_from(snap.get("ls_side"), snap.get("spot"), snap.get("strike"))
            if buy is not None and tl_fav is not None:
                fav_total += 1
                fav_agree += (buy["side"] == tl_fav)
        elif traded and result is None:
            n_no_result += 1            # ours but not settled yet -> skip
            continue
        elif client is not None:
            try:
                result = client.get_market(snap["ticker"])["market"].get("result")
            except Exception:
                result = None
            if result in ("yes", "no"):
                n_api += 1
            else:
                n_no_result += 1
                continue
        else:
            n_no_result += 1            # untraded, no --api -> leave for the engine
            continue

        row = build_outcome_row(asset, open_ts, snap, result, traded, now,
                                official_strike=None)
        if not row:
            n_bad += 1
            continue
        wins += row["fav_won"]
        if args.commit:
            rl.record_outcome(row)
        n_written += 1

    rl.flush()
    led.close()
    print(f"\n{'WROTE' if args.commit else 'WOULD WRITE'}: {n_written} labels "
          f"(ledger={n_ledger}, api={n_api})")
    print(f"skipped: no_result/unsettled={n_no_result}, no_snapshot={n_no_snap}, "
          f"unbuildable={n_bad}")
    if n_written:
        print(f"favorite win rate in backfilled labels: {wins/n_written:.1%}")
    if fav_total:
        print(f"cross-check (traded windows): timeline-favorite == ledger-bought side "
              f"in {fav_agree}/{fav_total} ({fav_agree/fav_total:.1%})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
