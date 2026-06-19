"""Bot configuration. Strategy: longshot-seller maker (see README).

The defaults below are illustrative starting points. Every value is overridable
from the environment (.env), which is where live parameters are actually set and
tuned per asset from offline research.
"""
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

PAPER_MODE = os.getenv("PAPER_MODE", "true").lower() != "false"

SERIES = {"btc": "KXBTC15M", "eth": "KXETH15M", "sol": "KXSOL15M",
          "xrp": "KXXRP15M", "doge": "KXDOGE15M"}
ASSETS = [a.strip() for a in os.getenv("ASSETS", "btc,eth,sol,xrp,doge").split(",")]

# --- longshot-seller maker params ---
MARGIN = float(os.getenv("MARGIN", "0.03"))          # base ask premium over model fair
MIN_LS_PRICE = float(os.getenv("MIN_LS_PRICE", "0.02"))
MAX_LS_PRICE = float(os.getenv("MAX_LS_PRICE", "0.20"))
MIN_MINUTE = float(os.getenv("MIN_MINUTE", "1"))
MAX_MINUTE = float(os.getenv("MAX_MINUTE", "13"))    # stop quoting after this
VOL_LOOKBACK_MIN = int(os.getenv("VOL_LOOKBACK_MIN", "120"))
STRIKE_VOL_BPS = float(os.getenv("STRIKE_VOL_BPS", "1.0"))

# --- dynamic margin by volatility regime ---
# Required premium scales up when short-horizon realized vol spikes above the
# rolling baseline (protects against adverse fills during fast moves) and is
# capped at MARGIN_MAX. Set MARGIN_MAX == MARGIN to disable.
MARGIN_MAX = float(os.getenv("MARGIN_MAX", "0.05"))
VOL_SPIKE_LOOKBACK_MIN = int(os.getenv("VOL_SPIKE_LOOKBACK_MIN", "15"))

# --- risk ---
# The favorite is bought at cost = 1 - longshot price (~$0.80-0.99/contract), so
# MAX_RISK_PER_WINDOW caps the dollars at stake per asset/window and
# MAX_DAILY_LOSS halts trading for the day. With KELLY_FRACTION=0 sizing is flat:
# every fill is CONTRACTS_PER_QUOTE. Keep these roughly in sync:
# MAX_RISK_PER_WINDOW ~ CONTRACTS_PER_QUOTE ($1/contract at risk); MAX_DAILY_LOSS
# ~ a handful of full single-asset losses at size.
CONTRACTS_PER_QUOTE = int(os.getenv("CONTRACTS_PER_QUOTE", "10"))   # flat size / Kelly ceiling
MAX_RISK_PER_WINDOW = float(os.getenv("MAX_RISK_PER_WINDOW", "10.0"))   # dollars
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "100.0"))            # halt for the day
PAPER_START_BALANCE = float(os.getenv("PAPER_START_BALANCE", "250.0"))

# Fraction of full Kelly to wager (0 = disable Kelly, fall back to a flat
# CONTRACTS_PER_QUOTE). Flat sizing is the safe default; enable Kelly only once a
# bucket's edge is stable enough out-of-sample to size into.
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0"))

# Skip quotes when favorite-side edge (model P - cost) is at or above this
# threshold in dollars. A wide inferred edge usually means the model is stale
# relative to the book rather than a real opportunity. Set 0 to disable.
MAX_EDGE = float(os.getenv("MAX_EDGE", "0.05"))

# When flat sizing took effect (unix seconds). The dashboard Performance tab can
# rebase an expected-equity curve from actual equity at this point.
_DEFAULT_FLAT_SINCE = datetime(2026, 1, 1, tzinfo=ZoneInfo("America/Chicago"))
FLAT_SINCE_TS = float(os.getenv("FLAT_SINCE_TS", str(int(_DEFAULT_FLAT_SINCE.timestamp()))))

# Bankroll allocated to this strategy at go-live, for the return calc on the
# dashboard. PnL is tracked purely from this bot's own ledger relative to this
# number (the exchange account balance may be shared across strategies).
START_BANKROLL = float(os.getenv("START_BANKROLL", "250.0"))

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "2.0"))
