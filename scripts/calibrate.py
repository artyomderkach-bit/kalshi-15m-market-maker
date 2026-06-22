"""Weekly model-calibration report for the 15-min crypto bot.

Reads the per-window outcomes the engine records to data/spot_history.db
(window_outcomes: model P(favorite) at a fixed reference point + the realized
result, for EVERY window — traded or not). Buckets by model probability and
compares model P against the realized win rate, so you can see whether the
model is systematically over/under-confident and roughly how to retune
STRIKE_VOL_BPS / MARGIN. It also contrasts traded vs non-traded windows to
surface selection bias.

This only reports suggestions — it never edits config automatically.

Writes data/calibration.txt and emails it. Run weekly by cron:
  CRON_TZ=America/Chicago
  0 10 * * 1 cd ~/kalshi-15m-bot && .venv/bin/python scripts/calibrate.py # kalshi-weekly-calibration
"""
import datetime
import os
import sqlite3
import sys
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except Exception:
    pass

from bot.emailer import send_email

DB = os.path.join(ROOT, "data", "spot_history.db")
REPORT = os.path.join(ROOT, "data", "calibration.txt")
CENTRAL = ZoneInfo("America/Chicago")
LOOKBACK_DAYS = int(os.getenv("CALIB_LOOKBACK_DAYS", "30"))
MIN_SAMPLE = int(os.getenv("CALIB_MIN_SAMPLE", "50"))
BUCKETS = [(0.50, 0.80), (0.80, 0.85), (0.85, 0.90),
           (0.90, 0.95), (0.95, 0.98), (0.98, 1.01)]
SUBJECT = "Kalshi 15m Crypto Bot — Weekly Model Calibration"


def load_rows():
    if not os.path.isfile(DB):
        return None
    since = int(datetime.datetime.now(datetime.timezone.utc).timestamp()) \
        - LOOKBACK_DAYS * 86400
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    try:
        rows = con.execute(
            "SELECT model_p_fav, fav_won, traded FROM window_outcomes "
            "WHERE open_ts >= ? AND model_p_fav IS NOT NULL AND fav_won IS NOT NULL",
            (since,)).fetchall()
    except sqlite3.Error:
        rows = []
    con.close()
    return rows


def summarize(rows):
    """Return (n, model_avg, realized_winrate, diff) over a row list."""
    n = len(rows)
    if not n:
        return 0, None, None, None
    model_avg = sum(r[0] for r in rows) / n
    realized = sum(r[1] for r in rows) / n
    return n, model_avg, realized, realized - model_avg


def bucket_table(rows):
    lines = [
        f"{'model P bucket':<16}{'n':>6}{'model':>9}{'realized':>10}{'diff':>8}",
        "-" * 49,
    ]
    for lo, hi in BUCKETS:
        sub = [r for r in rows if lo <= r[0] < hi]
        n, mp, rz, df = summarize(sub)
        if n == 0:
            lines.append(f"{f'[{lo:.2f},{hi:.2f})':<16}{0:>6}{'—':>9}{'—':>10}{'—':>8}")
        else:
            lines.append(
                f"{f'[{lo:.2f},{hi:.2f})':<16}{n:>6}{mp*100:>8.1f}%"
                f"{rz*100:>9.1f}%{df*100:>+7.1f}")
    return lines


def recommendation(diff, n):
    """diff = realized − model (in probability). Negative = model overconfident
    (favorite wins less than the model says) → it is underpricing tail risk."""
    if n < MIN_SAMPLE:
        return (f"Not enough settled windows yet ({n} < {MIN_SAMPLE}); "
                "collect more data before retuning.")
    pp = diff * 100.0
    if abs(pp) < 1.0:
        return ("Model is well calibrated (within 1pp of realized). "
                "No change recommended.")
    if pp < 0:
        return (f"Model is OVERCONFIDENT by {-pp:.1f}pp — favorites win less "
                "often than predicted. Pull probabilities toward 0.5 by "
                "RAISING STRIKE_VOL_BPS (more index/strike noise), and/or "
                "RAISE MARGIN so you demand more premium per bet. "
                f"As a rough guard, consider MARGIN ≈ current + {(-pp/100):.3f}.")
    return (f"Model is UNDERCONFIDENT by {pp:.1f}pp — favorites win more often "
            "than predicted. You may LOWER STRIKE_VOL_BPS slightly and/or "
            "LOWER MARGIN to capture more volume, but verify the trend is "
            "stable across several weeks first.")


def build_report():
    now_c = datetime.datetime.now(CENTRAL)
    rows = load_rows()
    if rows is None:
        return ("Weekly model calibration\n"
                f"Generated: {now_c:%Y-%m-%d %I:%M %p} Central\n\n"
                "No spot_history.db found yet — the engine writes window "
                "outcomes there once it has run through full 15-min windows.\n")

    n, mp, rz, df = summarize(rows)
    traded = [r for r in rows if r[2]]
    skipped = [r for r in rows if not r[2]]
    tn, tmp, trz, tdf = summarize(traded)
    sn, smp, srz, sdf = summarize(skipped)

    lines = [
        "Weekly model calibration",
        f"Generated: {now_c:%Y-%m-%d %I:%M %p} Central",
        f"Lookback:  {LOOKBACK_DAYS} days   Windows settled: {n}",
        "",
    ]
    if n:
        lines += [
            f"Overall: model avg {mp*100:.1f}%  vs realized win {rz*100:.1f}%  "
            f"(diff {df*100:+.1f}pp)",
            "",
            "Calibration by model-probability bucket:",
            *bucket_table(rows),
            "",
            "Selection (did our entry filter help or hurt?):",
            f"  Traded windows  ({tn:>4}): model {tmp*100:.1f}% / "
            f"realized {trz*100:.1f}% ({tdf*100:+.1f}pp)" if tn else
            "  Traded windows  (0): none",
            f"  Skipped windows ({sn:>4}): model {smp*100:.1f}% / "
            f"realized {srz*100:.1f}% ({sdf*100:+.1f}pp)" if sn else
            "  Skipped windows (0): none",
            "",
            "Recommendation:",
            f"  {recommendation(df, n)}",
        ]
    else:
        lines.append("No settled windows in the lookback window yet.")
    return "\n".join(lines) + "\n"


def main():
    report = build_report()
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w") as f:
        f.write(report)
    status = send_email(f"{SUBJECT} — {datetime.datetime.now(CENTRAL):%b %d}", report)
    print(report)
    print(status)


if __name__ == "__main__":
    main()
