"""Fair-value model for 15-min up/down markets.

Single source of truth: the backtest scripts (spot_model, longshot_maker,
maker_model, alt_battery) import `p_up` from here so the engine and the research
backtests can never drift apart.

P(up) = Phi( ln(S_t / strike) / sigma_eff ), where sigma_eff scales the realized
1-minute volatility by the time remaining in the window and adds an
index-tracking noise term.

NOTE: this public version uses a plain diffusion-to-close variance. The
production model uses a settlement-aware variance term (these contracts settle
on a trailing average of the index rather than the instantaneous price), which
is omitted here.
"""
import math


def p_up(s_now: float, strike: float, sigma_1m: float, seconds_left: float,
         strike_vol_bps: float = 1.0) -> float:
    """seconds_left: time until window close."""
    if seconds_left <= 0:
        return 1.0 if s_now >= strike else 0.0
    # Diffusion variance of the log-return over the time remaining (in minutes).
    minutes_left = seconds_left / 60.0
    var_min = max(minutes_left, 1e-9)
    sig_tau = sigma_1m * math.sqrt(var_min)
    sig_eff = math.sqrt(sig_tau ** 2 + (strike_vol_bps / 1e4) ** 2)
    z = math.log(s_now / strike) / sig_eff
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2)))


def fee(price: float, contracts: float = 1.0, rate: float = 0.07) -> float:
    """Kalshi fee, rounded up to next cent per execution."""
    raw = rate * contracts * price * (1.0 - price)
    return math.ceil(raw * 100.0 - 1e-9) / 100.0


def kelly_fraction(p: float, cost: float) -> float:
    """Full-Kelly bankroll fraction for buying the favorite at `cost` per
    contract with win probability `p` (binary payout of 1).

    Stake = cost, net win = (1 - cost), net loss = cost, so net odds
    b = (1 - cost) / cost and f* = (b·p − (1−p)) / b. Clamped to [0, 1];
    returns 0 when the bet is non-positive-EV.
    """
    if not (0.0 < cost < 1.0):
        return 0.0
    b = (1.0 - cost) / cost
    f = (b * p - (1.0 - p)) / b
    return max(0.0, min(1.0, f))


def favorite_edge(model_p_fav: float, cost_fav: float) -> float:
    """Favorite-side edge at quote time: model P(favorite) − cost to buy it."""
    return model_p_fav - cost_fav


def edge_exceeds_max(model_p_fav: float, cost_fav: float, max_edge: float) -> bool:
    """True when inferred edge is at or above ``max_edge`` (``max_edge <= 0`` disables)."""
    if max_edge <= 0:
        return False
    return favorite_edge(model_p_fav, cost_fav) >= max_edge
