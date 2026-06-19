"""Reconstruct ``window_outcomes`` (the supervised label) from persisted state.

The label used to be written only from the engine's in-memory ``snaps`` dict,
which is never rehydrated on restart -- so it silently stopped after the first
window (see ``settle_windows``). These helpers rebuild each label from a
``window_timeline`` snapshot (which IS persisted) plus the settled result, so the
label survives restarts and can be backfilled offline.

Pure functions only (no I/O): shared by the engine sweep and
``scripts/backfill_outcomes.py`` and unit-tested in ``tests/test_bot.py``.
"""

# Columns pulled from window_timeline to form a snapshot dict.
TIMELINE_COLS = (
    "minute", "ticker", "strike", "spot", "sigma_long", "sigma_short",
    "vol_ratio", "margin_eff", "yes_ask", "no_ask", "ls_side", "ls_price",
    "prem", "model_p_fav", "secs_left",
)


def fav_side_from(ls_side, spot, strike):
    """The favorite (model-likely) side of a window.

    Mirrors engine ``_evaluate``: the favorite is the side opposite the longshot
    (``fav_side = "no" if ls_side == "yes" else "yes"``). When ``ls_side`` is
    missing (a timeline row logged before the book was read), fall back to the
    model's directional call, which is ``yes`` iff spot is at/above the strike.
    Returns ``None`` only when neither is available, so we never emit a guess.
    """
    if ls_side == "yes":
        return "no"
    if ls_side == "no":
        return "yes"
    if spot is not None and strike is not None:
        return "yes" if spot >= strike else "no"
    return None


def build_outcome_row(asset, open_ts, snap, result, traded, recorded_ts,
                      official_strike=None):
    """Assemble a ``window_outcomes`` row from a timeline snapshot + settle result.

    ``snap`` is a dict keyed by :data:`TIMELINE_COLS`. ``result`` is ``"yes"`` or
    ``"no"``. Returns ``None`` (caller skips the write) when the favorite side or
    result is undeterminable, so a wrong/partial label is never recorded.
    """
    if result not in ("yes", "no"):
        return None
    fav = fav_side_from(snap.get("ls_side"), snap.get("spot"), snap.get("strike"))
    if fav is None:
        return None
    return {
        "asset": asset,
        "open_ts": int(open_ts),
        "ticker": snap.get("ticker"),
        "strike": snap.get("strike"),
        "spot_ref": snap.get("spot"),
        "sigma": snap.get("sigma_long"),
        "secs_left_ref": snap.get("secs_left"),
        "fav_side": fav,
        "model_p_fav": snap.get("model_p_fav"),
        "traded": int(bool(traded)),
        "result": result,
        "fav_won": 1 if result == fav else 0,
        "recorded_ts": int(recorded_ts),
        "sigma_short": snap.get("sigma_short"),
        "vol_ratio": snap.get("vol_ratio"),
        "margin_eff": snap.get("margin_eff"),
        "prem_at_snap": snap.get("prem"),
        "yes_ask_at_snap": snap.get("yes_ask"),
        "no_ask_at_snap": snap.get("no_ask"),
        "ls_side_at_snap": snap.get("ls_side"),
        "ls_price_at_snap": snap.get("ls_price"),
        "floor_strike": official_strike,
    }
