import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.kalshi_client import (  # noqa: E402
    longshot_sell_v2_body,
    parse_fill_count,
    parse_order_id,
)


def test_longshot_sell_v2_yes_side():
    body = longshot_sell_v2_body("TICK", "yes", 0.12, 50, "coid-1")
    assert body["ticker"] == "TICK"
    assert body["side"] == "ask"
    assert body["price"] == "0.1200"
    assert body["count"] == "50.00"
    assert body["post_only"] is True
    assert body["time_in_force"] == "good_till_canceled"
    assert body["self_trade_prevention_type"] == "taker_at_cross"


def test_longshot_sell_v2_no_side():
    body = longshot_sell_v2_body("TICK", "no", 0.10, 25, "coid-2")
    assert body["side"] == "bid"
    assert body["price"] == "0.9000"   # sell NO@0.10 == bid YES@0.90


def test_parse_order_id_v2_and_v1():
    assert parse_order_id({"order_id": "abc"}) == "abc"
    assert parse_order_id({"order": {"order_id": "legacy"}}) == "legacy"
    assert parse_order_id({}) is None


def test_parse_fill_count_shapes():
    assert parse_fill_count({"fill_count_fp": "12.50"}) == 12.5
    assert parse_fill_count({"fill_count": "3.00"}) == 3.0
    assert parse_fill_count({"taker_fill_count": 2, "maker_fill_count": 1}) == 3.0
    assert parse_fill_count({}) == 0.0
