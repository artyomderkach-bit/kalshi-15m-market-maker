import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.model import p_up, fee, favorite_edge, edge_exceeds_max  # noqa: E402
from bot.outcomes import fav_side_from, build_outcome_row  # noqa: E402


def test_fee_rounding_up():
    # 0.07 * 1 * 0.5 * 0.5 = 0.0175 -> 2 cents
    assert fee(0.50, 1) == 0.02
    # 0.07 * 1 * 0.05 * 0.95 = 0.0033 -> 1 cent
    assert fee(0.05, 1) == 0.01
    # 100 contracts at 0.50: 1.75 exactly
    assert fee(0.50, 100) == 1.75
    # maker rate
    assert fee(0.50, 1, rate=0.0175) == 0.01


def test_p_up_basic():
    # price exactly at strike, plenty of time -> ~0.5
    p = p_up(100.0, 100.0, sigma_1m=0.001, seconds_left=600)
    assert abs(p - 0.5) < 0.01
    # price well above strike, little time -> near 1
    p = p_up(101.0, 100.0, sigma_1m=0.001, seconds_left=120)
    assert p > 0.99
    # symmetric
    p_hi = p_up(100.5, 100.0, sigma_1m=0.001, seconds_left=300)
    p_lo = p_up(99.5022, 100.0, sigma_1m=0.001, seconds_left=300)  # ~ -0.5% in log
    assert abs((1 - p_lo) - p_hi) < 0.02


def test_p_up_lock_in():
    # within final minute uncertainty collapses
    p_mid = p_up(100.05, 100.0, sigma_1m=0.001, seconds_left=450)
    p_late = p_up(100.05, 100.0, sigma_1m=0.001, seconds_left=30)
    assert p_late > p_mid
    assert p_up(100.0, 100.0, sigma_1m=0.001, seconds_left=0) == 1.0  # >= strike wins


def test_p_up_monotone_in_price():
    ps = [p_up(100.0 * (1 + d), 100.0, 0.001, 300) for d in (-0.002, -0.001, 0, 0.001, 0.002)]
    assert ps == sorted(ps)


def test_fav_side_from():
    # favorite is the side opposite the longshot (mirrors engine _evaluate)
    assert fav_side_from("yes", None, None) == "no"
    assert fav_side_from("no", None, None) == "yes"
    # fallback to spot-vs-strike when the longshot side is missing
    assert fav_side_from(None, 101.0, 100.0) == "yes"
    assert fav_side_from(None, 99.0, 100.0) == "no"
    assert fav_side_from(None, 100.0, 100.0) == "yes"   # at strike -> up
    # neither source available -> no guess
    assert fav_side_from(None, None, None) is None


SNAP = {
    "minute": 7, "ticker": "KXBTC15M-X", "strike": 100.0, "spot": 101.0,
    "sigma_long": 0.001, "sigma_short": 0.0012, "vol_ratio": 1.2,
    "margin_eff": 0.02, "yes_ask": 0.90, "no_ask": 0.12, "ls_side": "no",
    "ls_price": 0.11, "prem": 0.03, "model_p_fav": 0.91, "secs_left": 480.0,
}


def test_build_outcome_row_label_and_mapping():
    row = build_outcome_row("btc", 1000, SNAP, "yes", traded=True,
                            recorded_ts=12345, official_strike=100.5)
    assert row["fav_side"] == "yes"          # opposite of ls_side="no"
    assert row["fav_won"] == 1               # result "yes" == favorite
    assert row["result"] == "yes" and row["traded"] == 1
    # snapshot fields map to the *_at_snap / _ref columns
    assert row["spot_ref"] == 101.0 and row["sigma"] == 0.001
    assert row["prem_at_snap"] == 0.03 and row["ls_side_at_snap"] == "no"
    assert row["yes_ask_at_snap"] == 0.90 and row["model_p_fav"] == 0.91
    assert row["floor_strike"] == 100.5 and row["recorded_ts"] == 12345


def test_build_outcome_row_favorite_loses():
    row = build_outcome_row("btc", 1000, SNAP, "no", traded=False, recorded_ts=1)
    assert row["fav_won"] == 0 and row["traded"] == 0


def test_build_outcome_row_guards():
    assert build_outcome_row("btc", 1, SNAP, "void", True, 1) is None      # bad result
    blank = {"ls_side": None, "spot": None, "strike": None}
    assert build_outcome_row("btc", 1, blank, "yes", True, 1) is None      # no favorite


def test_favorite_edge_equals_longshot_premium():
    # model_p_fav - cost_fav == ls_price - model_fair (longshot prem)
    assert favorite_edge(0.91, 0.89) == pytest.approx(0.02)


def test_edge_exceeds_max():
    assert not edge_exceeds_max(0.91, 0.89, 0.03)   # 2¢ edge, 3¢ cap
    assert edge_exceeds_max(0.94, 0.89, 0.03)       # 5¢ edge
    assert not edge_exceeds_max(0.94, 0.89, 0.0)    # disabled
    assert edge_exceeds_max(0.92, 0.89, 0.03)       # exactly 3¢ blocks


# --- cancel-race A/B harness ---------------------------------------------------
from bot import ab  # noqa: E402


def test_ab_arm_stable_and_split():
    # deterministic per (asset, open_ts)
    assert ab.arm_for("btc", 1781480700) == ab.arm_for("btc", 1781480700)
    arms = [ab.arm_for(a, t)
            for a in ("btc", "eth", "sol", "xrp", "doge")
            for t in range(1781480700, 1781480700 + 900 * 300, 900)]
    # three arms, each roughly 1/3 (stable hash mod 3)
    for arm in ("A", "B", "C"):
        frac = arms.count(arm) / len(arms)
        assert 0.27 < frac < 0.40, (arm, frac)


def test_ab_replace_params():
    # B is stickier than control (fewer reprices); C is faster (more reprices)
    a_thr, a_book = ab.replace_params("A")
    b_thr, b_book = ab.replace_params("B")
    c_thr, c_book = ab.replace_params("C")
    assert (a_thr, a_book) == (ab.CTRL_REPLACE_THRESHOLD, ab.CTRL_MIN_IN_BOOK_SEC)
    assert b_thr >= a_thr and b_book >= a_book   # sticky
    assert c_thr <= a_thr and c_book <= a_book   # fast


def test_ab_effective_arm_off_is_control(monkeypatch):
    monkeypatch.setattr(ab, "AB_TEST", False)
    assert all(ab.effective_arm(a, 1781480700) == "A"
               for a in ("btc", "eth", "sol"))


def test_ab_is_through_lift():
    # yes-longshot lifted through 0.10 by a yes-taker paying 0.12 -> pickoff
    assert ab.is_through_lift("yes", 0.10, yes_price=0.12, no_price=0.88,
                              taker_side="yes")
    # exactly at level is not "through"
    assert not ab.is_through_lift("yes", 0.12, 0.12, 0.88, "yes")
    # wrong taker side does not count
    assert not ab.is_through_lift("yes", 0.10, 0.12, 0.88, "no")
    # no-longshot side uses no_price
    assert ab.is_through_lift("no", 0.10, yes_price=0.85, no_price=0.13,
                              taker_side="no")
