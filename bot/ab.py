"""A/B harness for the requote (cancel-race) policy (3-way).

Motivation: a maker can be filled on a *stale* quote when an order is lifted
during a cancel/replace. There are two opposite ways to fight that, with no
a-priori winner:

  - FAST (arm C): reprice eagerly so the quote is always near fair -> small loss
    per bad fill, but more cancel/replace churn (more race windows, more chasing).
  - STICKY (arm B): reprice rarely -> fewer cancel races and no chasing, but the
    quote can be more stale when it is hit.

So we race three policies on disjoint windows simultaneously:
  A = control (today's requote policy), B = sticky, C = fast.
Arm assignment is a stable per-window hash so all three see the same markets.
Paper mode cannot realize partial fills; the dashboard measures *exposure*
(replace churn + tape prints through a just-cancelled level) and PnL per arm.
On the live box these become real per-arm economics.

NOTE: the bot's POLL_SECONDS (~2s) is global, not per-arm -- the real latency
floor. Arm C only tunes how aggressively it reprices *within* that 2s loop, so it
tests the *direction* of the speed hypothesis, not true low-latency making.

All knobs are env-overridable. With ``AB_TEST`` off the engine uses control
params for every window, so behaviour is identical to before.
"""
import os
import zlib

# Importing config runs load_dotenv() at module load. ab is imported *before*
# config in engine.py's `from . import ab, config`, so without this the AB_*
# os.getenv reads below would fire before .env is loaded and always see defaults.
from . import config  # noqa: F401

AB_TEST = os.getenv("AB_TEST", "false").lower() == "true"

# Arm A -- control. MUST mirror the engine's historical hardcoded defaults so
# arm A == pre-harness behaviour.
CTRL_REPLACE_THRESHOLD = 0.02   # min |Δ longshot price| to cancel+replace
CTRL_MIN_IN_BOOK_SEC = 3.0      # min seconds resting before a replace

# Arm B -- "sticky": reprice less often => fewer cancel races, no chasing.
AB_REPLACE_THRESHOLD = float(os.getenv("AB_REPLACE_THRESHOLD", "0.04"))
AB_MIN_IN_BOOK_SEC = float(os.getenv("AB_MIN_IN_BOOK_SEC", "12"))

# Arm C -- "fast": reprice on the smallest move with no cooldown => freshest
# (least stale) quote, at the cost of more churn. 0.01 = one cent (price floor).
AB_FAST_REPLACE_THRESHOLD = float(os.getenv("AB_FAST_REPLACE_THRESHOLD", "0.01"))
AB_FAST_MIN_IN_BOOK_SEC = float(os.getenv("AB_FAST_MIN_IN_BOOK_SEC", "0"))

# A tape print within this many seconds after a cancel that goes through the
# cancelled level counts as a would-be cancel-race pickoff.
AB_EXPOSURE_SEC = float(os.getenv("AB_EXPOSURE_SEC", "4"))

ARMS = ("A", "B", "C")
ARM_LABEL = {"A": "A (control)", "B": "B (sticky)", "C": "C (fast)"}


def arm_for(asset: str, open_ts) -> str:
    """Stable per-window arm: 'A' control / 'B' sticky / 'C' fast.

    Deterministic so the engine and dashboard agree without persisting the
    assignment, and split by window so all three arms face the same market
    regimes over time on disjoint windows.
    """
    h = zlib.crc32(f"{asset}:{int(open_ts)}".encode())
    return ARMS[h % 3]


def effective_arm(asset: str, open_ts) -> str:
    """Arm actually applied: always 'A' when the harness is disabled."""
    return arm_for(asset, open_ts) if AB_TEST else "A"


def replace_params(arm: str):
    """(min_price_move_to_replace, min_seconds_in_book) for an arm."""
    if arm == "B":
        return AB_REPLACE_THRESHOLD, AB_MIN_IN_BOOK_SEC
    if arm == "C":
        return AB_FAST_REPLACE_THRESHOLD, AB_FAST_MIN_IN_BOOK_SEC
    return CTRL_REPLACE_THRESHOLD, CTRL_MIN_IN_BOOK_SEC


def is_through_lift(ls_side, level, yes_price, no_price, taker_side) -> bool:
    """True if a tape print lifted the longshot *through* our resting offer.

    Our post-only offer sells the longshot at ``level`` (the longshot's own price
    space). A pickoff is a buyer (taker on the longshot side) paying strictly
    more than ``level`` for the longshot.
    """
    if ls_side not in ("yes", "no") or level is None:
        return False
    if taker_side is not None and taker_side != ls_side:
        return False
    px = yes_price if ls_side == "yes" else no_price
    if px is None:
        return False
    return px > level + 1e-9
