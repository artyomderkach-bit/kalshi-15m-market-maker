"""Maker engine: sell overpriced longshots on 15-min crypto markets.

Per asset/window: when the longshot side's ask exceeds model fair value by
MARGIN, maintain a post-only offer at the ask (= maker-buy of the favorite).
Paper mode detects fills from the public trades feed (price printing through
our level => guaranteed fill; touch prints recorded as shadow stats).
Live mode places real post-only orders. One fill per asset/window.
"""
import datetime
import os
import time
import traceback

from . import ab, config
from .db import connect, log_event, record_trade
from .kalshi_client import KalshiClient, parse_fill_count, parse_order_id
from .model import p_up, fee, kelly_fraction, edge_exceeds_max, favorite_edge
from .outcomes import build_outcome_row
from .research import ResearchLog, TIMELINE_MINUTES
from .spot_feed import SpotFeed

WINDOW_SEC = 900
MAKER_RATE = 0.0175
# Persist the trade tape for the current + previous window (so prints near
# settlement are captured), polled at most this often per asset.
TRADES_POLL_SEC = float(os.getenv("TRADES_POLL_SEC", "12"))
# Supervised-label sweep: heal recent windows that lack a window_outcomes row
# (e.g. after a restart) by reading the persisted timeline + settle result.
# Throttled and capped so the result lookups never stall the trade loop.
OUTCOME_SWEEP_SEC = float(os.getenv("OUTCOME_SWEEP_SEC", "20"))
OUTCOME_LOOKBACK_SEC = int(os.getenv("OUTCOME_LOOKBACK_SEC", str(6 * 3600)))
OUTCOME_SWEEP_MAX = int(os.getenv("OUTCOME_SWEEP_MAX", "30"))


def now_window():
    return int(time.time() // WINDOW_SEC) * WINDOW_SEC


def _norm_levels(levels):
    """Normalize a Kalshi bid ladder to [[price_dollars, size], ...].
    Accepts cents or dollar-denominated levels; sorted best (highest) first."""
    out = []
    for l in levels:
        try:
            p = float(l[0])
        except (TypeError, ValueError, IndexError):
            continue
        sz = None
        if len(l) > 1 and l[1] is not None:
            try:
                sz = float(l[1])
            except (TypeError, ValueError):
                sz = None
        if p > 1.0:
            p /= 100.0
        out.append([round(p, 4), sz])
    out.sort(key=lambda x: x[0], reverse=True)
    return out


class Quote:
    __slots__ = ("asset", "open_ts", "ticker", "ls_side", "price", "qty",
                 "order_id", "placed_ts", "last_trade_ts", "fair")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


class Engine:
    def __init__(self):
        self.assets = [a for a in config.ASSETS if a in config.SERIES]
        self.client = KalshiClient(min_interval=0.10)
        self.spot = SpotFeed(self.assets, poll_seconds=1.0,
                             vol_lookback_min=config.VOL_LOOKBACK_MIN,
                             persist=True)
        self.con = connect()
        self.research = ResearchLog()
        self.mode = "paper" if config.PAPER_MODE else "live"
        self.strikes = {}      # (asset, open_ts) -> strike used by model
        self.official_strikes = {}  # (asset, open_ts) -> Kalshi floor_strike
        self.tickers = {}      # (asset, open_ts) -> ticker
        self.quotes = {}       # (asset, open_ts) -> Quote
        self.filled = {}       # (asset, open_ts) -> position dict
        self.snaps = {}        # (asset, open_ts) -> model snapshot (all windows)
        self._book_cache = {}  # (asset, open_ts) -> (ts, eval row) last book fetch
        self._trade_cursor = {}    # (asset, open_ts) -> last trade ts persisted
        self._last_trade_poll = {} # (asset, open_ts) -> last tape-poll ts
        self._last_outcome_sweep = 0.0  # throttle for settle_windows label sweep
        self.traded_windows = set()  # (asset, open_ts) we placed/filled a bet on
        self.touch_stats = {"touch": 0, "through": 0}
        self.daily_pnl = 0.0
        self.daily_date = None
        self.halted = False
        self.bankroll = config.START_BANKROLL  # refreshed in record_equity
        self.diag = {}   # asset -> last skip/quote reason (for heartbeat)

    def _vol_context(self, asset, sigma_long):
        sigma_short = self.spot.sigma_1m_window(asset, config.VOL_SPIKE_LOOKBACK_MIN)
        vol_ratio = (sigma_short / sigma_long
                     if sigma_short and sigma_long and sigma_long > 0 else None)
        margin_eff = self._margin_eff(asset, sigma_long)
        return sigma_short, vol_ratio, margin_eff

    # ---------- discovery / strike ----------
    def find_ticker(self, asset, open_ts):
        key = (asset, open_ts)
        if key in self.tickers:
            return self.tickers[key]
        try:
            r = self.client.get_markets(config.SERIES[asset], status="open", limit=10)
        except Exception:
            return None
        for m in r.get("markets", []):
            ct = int(datetime.datetime.fromisoformat(
                m["close_time"].replace("Z", "+00:00")).timestamp())
            if ct == open_ts + WINDOW_SEC:
                self.tickers[key] = m["ticker"]
                fs = m.get("floor_strike")
                if fs:
                    self.official_strikes[key] = float(fs)
                return m["ticker"]
        return None

    def capture_strike(self, asset, open_ts):
        """Use Kalshi floor_strike (settlement reference). Coinbase 60s avg only
        as fallback when floor_strike is unavailable (early in window)."""
        key = (asset, open_ts)
        if key in self.strikes:
            return self.strikes[key]
        self.find_ticker(asset, open_ts)
        if key in self.official_strikes:
            self.strikes[key] = self.official_strikes[key]
            return self.strikes[key]
        if time.time() > open_ts + 3:
            s = self.spot.avg_60s(asset, end_ts=open_ts)
            if s:
                self.strikes[key] = s
        return self.strikes.get(key)

    # ---------- quoting ----------
    def _skip(self, asset, reason):
        self.diag[asset] = reason

    def _arm(self, asset, open_ts):
        """Cancel-race A/B arm for this window ('A' control / 'B' sticky).
        Always 'A' when the harness is disabled."""
        return ab.effective_arm(asset, open_ts)

    def _evaluate(self, asset, open_ts, fetch_book=True):
        """Evaluate one asset/window. Returns a feature dict plus optional quote
        tuple (ticker, ls_side, price, fair). Does not place orders."""
        minute = (time.time() - open_ts) / 60.0
        row = {
            "ts": int(time.time()),
            "asset": asset,
            "open_ts": open_ts,
            "minute": round(minute, 2),
            "action": "skip",
            "skip_reason": None,
        }
        if minute < config.MIN_MINUTE:
            self._skip(asset, f"wait m={minute:.0f}")
            row["skip_reason"] = "wait"
            return row
        if minute > config.MAX_MINUTE:
            self._skip(asset, f"late m={minute:.0f}")
            row["skip_reason"] = "late"
            return row
        strike = self.capture_strike(asset, open_ts)
        s_now = self.spot.price(asset)
        sig = self.spot.sigma_1m(asset)
        row["strike"] = strike
        row["spot"] = s_now
        row["sigma_long"] = sig
        if not strike:
            self._skip(asset, "no strike")
            row["skip_reason"] = "no_strike"
            return row
        if not s_now:
            self._skip(asset, "no spot")
            row["skip_reason"] = "no_spot"
            return row
        if not sig:
            self._skip(asset, "no vol")
            row["skip_reason"] = "no_vol"
            return row
        sigma_short, vol_ratio, margin_eff = self._vol_context(asset, sig)
        row["sigma_short"] = sigma_short
        row["vol_ratio"] = vol_ratio
        row["margin_eff"] = margin_eff
        ticker = self.find_ticker(asset, open_ts)
        row["ticker"] = ticker
        if not ticker:
            self._skip(asset, "no ticker")
            row["skip_reason"] = "no_ticker"
            return row
        if not fetch_book:
            secs_left = open_ts + WINDOW_SEC - time.time()
            pu = p_up(s_now, strike, sig, secs_left, config.STRIKE_VOL_BPS)
            fav_side = "yes" if pu >= 0.5 else "no"
            row["fav_side"] = fav_side
            row["model_p_fav"] = pu if fav_side == "yes" else 1.0 - pu
            row["secs_left"] = secs_left
            row["skip_reason"] = "timeline"
            return row
        try:
            r = self.client.get_orderbook(ticker)
        except Exception as e:
            self._skip(asset, f"ob err {e}")
            row["skip_reason"] = "ob_err"
            return row
        ob = r.get("orderbook_fp") or r.get("orderbook") or {}
        yes_bids = ob.get("yes_dollars") or ob.get("yes") or []
        no_bids = ob.get("no_dollars") or ob.get("no") or []
        if not yes_bids or not no_bids:
            self._skip(asset, "empty book")
            row["skip_reason"] = "empty_book"
            return row
        best_yes_bid = max(float(l[0]) for l in yes_bids)
        best_no_bid = max(float(l[0]) for l in no_bids)
        if best_yes_bid > 1.0:
            best_yes_bid /= 100.0
            best_no_bid /= 100.0
        yes_ask = 1.0 - best_no_bid
        no_ask = 1.0 - best_yes_bid
        row.update(yes_bid=best_yes_bid, yes_ask=yes_ask,
                   no_bid=best_no_bid, no_ask=no_ask,
                   yes_book=_norm_levels(yes_bids),
                   no_book=_norm_levels(no_bids))
        self._book_cache[(asset, open_ts)] = (time.time(), row)

        secs_left = open_ts + WINDOW_SEC - time.time()
        pu = p_up(s_now, strike, sig, secs_left, config.STRIKE_VOL_BPS)
        if yes_ask <= no_ask:
            ls_side, A, fair = "yes", yes_ask, pu
        else:
            ls_side, A, fair = "no", no_ask, 1.0 - pu
        prem = A - fair
        fav_side = "no" if ls_side == "yes" else "yes"
        model_p_fav = 1.0 - fair
        cost_fav = 1.0 - A
        row.update(ls_side=ls_side, ls_price=A, model_fair=fair, prem=prem,
                   fav_side=fav_side, model_p_fav=model_p_fav, cost_fav=cost_fav,
                   secs_left=secs_left)

        if A < config.MIN_LS_PRICE:
            self._skip(asset, f"ls={A:.2f}<{config.MIN_LS_PRICE}")
            row["skip_reason"] = "ls_range"
            return row
        if A > config.MAX_LS_PRICE:
            self._skip(asset, f"ls={A:.2f}>{config.MAX_LS_PRICE}")
            row["skip_reason"] = "ls_range"
            return row
        if prem < margin_eff:
            self._skip(asset, f"prem={prem:+.2f}<{margin_eff:.2f}")
            row["skip_reason"] = "prem"
            return row
        edge_fav = favorite_edge(model_p_fav, cost_fav)
        row["edge_fav"] = edge_fav
        if edge_exceeds_max(model_p_fav, cost_fav, config.MAX_EDGE):
            self._skip(asset, f"edge={edge_fav:+.2f}>={config.MAX_EDGE:.2f}")
            row["skip_reason"] = "max_edge"
            return row
        self._skip(asset, f"QUOTE {ls_side}@{A:.2f} prem={prem:+.2f} m={margin_eff:.2f}")
        row["action"] = "quote"
        row["skip_reason"] = None
        row["quote"] = (ticker, ls_side, round(A, 2), fair)
        return row

    def desired_quote(self, asset, open_ts):
        """Return (ticker, ls_side, price, fair) or None."""
        ev = self._evaluate(asset, open_ts, fetch_book=True)
        return ev.get("quote")

    def _margin_eff(self, asset, sigma_long):
        """Required premium, widened when short-horizon vol spikes above the
        full-lookback baseline. Capped at MARGIN_MAX; == MARGIN if disabled or
        data is insufficient."""
        base = config.MARGIN
        if config.MARGIN_MAX <= base:
            return base
        sigma_short = self.spot.sigma_1m_window(asset, config.VOL_SPIKE_LOOKBACK_MIN)
        if not sigma_short or not sigma_long or sigma_long <= 0:
            return base
        ratio = sigma_short / sigma_long
        return min(config.MARGIN_MAX, base * max(1.0, ratio))

    def manage_quotes(self):
        open_ts = now_window()
        for asset in self.assets:
            key = (asset, open_ts)
            if key in self.filled or self.halted:
                self.cancel_quote(key, reason="filled" if key in self.filled else "halted")
                continue
            ev = self._evaluate(asset, open_ts, fetch_book=True)
            want = ev.get("quote")
            cur = self.quotes.get(key)
            if want is None:
                self.research.log_decision(ev)
                self.cancel_quote(key, reason="no_quote")
                continue
            ticker, ls_side, price, fair = want
            cost = 1.0 - price
            qty = self._size_quote(cost, fair)
            ev["kelly_qty"] = qty
            if qty < 1:
                ev["action"] = "skip"
                ev["skip_reason"] = "size"
                self._skip(asset, f"size<1 (thin edge) cost={cost:.2f}")
                self.research.log_decision(ev)
                self.cancel_quote(key, reason="size")
                continue
            self.traded_windows.add(key)
            ev["action"] = "quote"
            self.research.log_decision(ev)
            # Requote-policy A/B: arm B reprices stickier (higher threshold,
            # longer min-in-book) to cut cancel/replace races; arm C reprices
            # faster. Arm A is the control requote policy.
            arm = self._arm(asset, open_ts)
            replace_thr, min_in_book = ab.replace_params(arm)
            if cur and cur.ls_side == ls_side and abs(cur.price - price) < replace_thr:
                continue
            if cur and cur.placed_ts and time.time() - cur.placed_ts < min_in_book:
                continue
            self.cancel_quote(key, reason="replace")
            order_id = None
            if self.mode == "live":
                coid = f"ls-{asset}-{open_ts}-{int(time.time()*1000) % 10**9}"
                try:
                    resp = self.client.create_longshot_sell(
                        ticker, ls_side, price, qty, coid)
                    order_id = parse_order_id(resp)
                    log_event(self.con, "info",
                              f"ORDER {ticker} sell {ls_side} x{qty} @ {price:.2f} "
                              f"(prem={price - fair:+.2f})")
                except Exception as e:
                    log_event(self.con, "error", f"order failed {ticker}: {e}")
                    continue
            placed_ts = time.time()
            self.quotes[key] = Quote(asset=asset, open_ts=open_ts, ticker=ticker,
                                     ls_side=ls_side, price=price, qty=qty,
                                     order_id=order_id, placed_ts=placed_ts,
                                     last_trade_ts=placed_ts, fair=fair)
            self.research.log_lifecycle({
                "ts": int(placed_ts), "asset": asset, "open_ts": open_ts,
                "ticker": ticker, "order_id": order_id, "event": "placed",
                "ls_side": ls_side, "price": price, "qty": qty, "fair": fair,
                "prem": price - fair, "arm": arm,
            })

    def _size_quote(self, cost, fair):
        """Contracts to quote. Kelly-fraction of bankroll off the favorite's
        edge, then capped by CONTRACTS_PER_QUOTE and MAX_RISK_PER_WINDOW. With
        KELLY_FRACTION=0 falls back to a flat CONTRACTS_PER_QUOTE."""
        if cost <= 0:
            return 0
        if config.KELLY_FRACTION > 0:
            p_fav = 1.0 - fair
            f = kelly_fraction(p_fav, cost)
            stake = f * config.KELLY_FRACTION * max(self.bankroll, 0.0)
            qty = int(stake / cost)
        else:
            qty = config.CONTRACTS_PER_QUOTE
        qty = min(qty, config.CONTRACTS_PER_QUOTE)
        if cost * qty > config.MAX_RISK_PER_WINDOW:
            qty = int(config.MAX_RISK_PER_WINDOW / cost)
        return qty

    def cancel_quote(self, key, reason="replace"):
        q = self.quotes.pop(key, None)
        if q and q.order_id and self.mode == "live":
            try:
                self.client.cancel_order(q.order_id)
            except Exception:
                pass
            if key not in self.filled:
                fc = self._order_fill_count(q.order_id)
                if fc:
                    self._record_fill(key, q, fc, fill_type="live-partial")
        if q:
            self.research.log_lifecycle({
                "ts": int(time.time()), "asset": q.asset, "open_ts": q.open_ts,
                "ticker": q.ticker, "order_id": q.order_id, "event": "cancel",
                "ls_side": q.ls_side, "price": q.price, "qty": q.qty,
                "fair": q.fair, "prem": (q.price - q.fair) if q.fair else None,
                "seconds_in_book": (time.time() - q.placed_ts) if q.placed_ts else None,
                "note": reason, "arm": self._arm(q.asset, q.open_ts),
            })

    # ---------- fills ----------
    def check_fills(self):
        for key, q in list(self.quotes.items()):
            if self.mode == "live":
                self._check_fill_live(key, q)
            else:
                self._check_fill_paper(key, q)

    def _order_fill_count(self, order_id):
        try:
            o = self.client.get_order(order_id).get("order", {})
        except Exception:
            return None
        fc = parse_fill_count(o)
        return fc if fc > 0 else None

    def _check_fill_live(self, key, q):
        filled_qty = self._order_fill_count(q.order_id)
        if filled_qty:
            self._record_fill(key, q, filled_qty, fill_type="live")

    def _check_fill_paper(self, key, q):
        try:
            r = self.client.get_trades(q.ticker, min_ts=q.last_trade_ts - 1, limit=100)
        except Exception:
            return
        q.last_trade_ts = time.time()
        for t in r.get("trades", []):
            ypd = t.get("yes_price_dollars")
            yp = float(ypd) if ypd is not None else (t.get("yes_price") or 0) / 100.0
            if not yp:
                continue
            created = t.get("created_time", "")
            try:
                ct = datetime.datetime.fromisoformat(
                    created.replace("Z", "+00:00")).timestamp()
                if ct < q.placed_ts:
                    continue
            except ValueError:
                pass
            # our offer: selling ls_side at q.price; only buyer-initiated flow
            # on the longshot side can hit it
            if t.get("taker_side") and t["taker_side"] != q.ls_side:
                continue
            level_yes = q.price if q.ls_side == "yes" else 1.0 - q.price
            through = (q.ls_side == "yes" and yp > level_yes + 0.001) or \
                      (q.ls_side == "no" and yp < level_yes - 0.001)
            touch = abs(yp - level_yes) <= 0.001
            if touch:
                self.touch_stats["touch"] += 1
            if through:
                self.touch_stats["through"] += 1
                self._record_fill(key, q, q.qty, fill_type="through")
                return

    def _record_fill(self, key, q, qty, fill_type):
        cost = 1.0 - q.price                       # we own the favorite at this cost
        fav_side = "no" if q.ls_side == "yes" else "yes"
        fees = fee(cost, qty, rate=MAKER_RATE)
        model_p = (1.0 - q.fair) if q.fair is not None else None
        edge = (model_p - cost) if model_p is not None else None
        self.traded_windows.add(key)
        self.filled[key] = {"ticker": q.ticker, "asset": q.asset,
                            "open_ts": q.open_ts, "side": fav_side,
                            "price": cost, "qty": qty, "fees": fees}
        record_trade(self.con, mode=self.mode, window_open_ts=q.open_ts,
                     ticker=q.ticker, asset=q.asset, side=fav_side, action="buy",
                     qty=qty, price=cost, fees=fees, order_id=q.order_id,
                     is_maker=1, model_p=model_p, edge=edge, note=fill_type)
        log_event(self.con, "info",
                  f"FILL {self.mode} {q.ticker} sold {q.ls_side} x{qty} @ {q.price:.2f} "
                  f"({fill_type})")
        self.research.log_lifecycle({
            "ts": int(time.time()), "asset": q.asset, "open_ts": q.open_ts,
            "ticker": q.ticker, "order_id": q.order_id,
            "event": "partial_fill" if fill_type == "live-partial" else "fill",
            "ls_side": q.ls_side, "price": q.price, "qty": q.qty, "fair": q.fair,
            "prem": (q.price - q.fair) if q.fair else None,
            "fill_qty": qty,
            "seconds_in_book": (time.time() - q.placed_ts) if q.placed_ts else None,
            "note": fill_type, "arm": self._arm(q.asset, q.open_ts),
        })
        self.research.log_decision({
            "ts": int(time.time()), "asset": q.asset, "open_ts": q.open_ts,
            "minute": round((time.time() - q.open_ts) / 60.0, 2),
            "ticker": q.ticker, "action": "fill", "skip_reason": None,
            "ls_side": q.ls_side, "ls_price": q.price, "model_fair": q.fair,
            "prem": (q.price - q.fair) if q.fair else None,
            "order_id": q.order_id,
        })
        self.cancel_quote(key, reason="filled")

    # ---------- settlement ----------
    def settle(self):
        now = time.time()
        for key in [k for k, p in self.filled.items()
                    if now > p["open_ts"] + WINDOW_SEC + 20 and not p.get("done")]:
            pos = self.filled[key]
            try:
                m = self.client.get_market(pos["ticker"])["market"]
            except Exception:
                continue
            result = m.get("result")
            if result not in ("yes", "no"):
                if now > pos["open_ts"] + WINDOW_SEC + 900:
                    pos["done"] = True  # give up tracking
                continue
            won = result == pos["side"]
            pnl = pos["qty"] * ((1.0 if won else 0.0) - pos["price"]) - pos["fees"]
            self.daily_pnl += pnl
            pos["done"] = True
            record_trade(self.con, mode=self.mode, window_open_ts=pos["open_ts"],
                         ticker=pos["ticker"], asset=pos["asset"], side=pos["side"],
                         action="settle", qty=pos["qty"],
                         price=1.0 if won else 0.0, fees=0.0, note=f"pnl={pnl:.2f}")
            log_event(self.con, "info",
                      f"SETTLE {pos['ticker']} {pos['side']} result={result} pnl=${pnl:.2f}")

    # ---------- per-window calibration capture (all windows, traded or not) ----------
    def track_windows(self):
        open_ts = now_window()
        now = time.time()
        minute = int((now - open_ts) / 60.0)
        for asset in self.assets:
            key = (asset, open_ts)
            if minute in TIMELINE_MINUTES:
                ev = self._evaluate(asset, open_ts, fetch_book=True)
                tl = {k: ev.get(k) for k in (
                    "asset", "open_ts", "ticker", "strike", "spot", "sigma_long",
                    "sigma_short", "vol_ratio", "margin_eff", "yes_ask", "no_ask",
                    "ls_side", "ls_price", "prem", "model_p_fav", "secs_left")}
                tl["minute"] = minute
                tl["ts"] = int(now)
                self.research.log_timeline(tl)
                self.research.log_orderbook(ev, phase="checkpoint")
                if minute == 7 and ev.get("model_p_fav") is not None:
                    self._store_snap(key, ev)
            elif minute >= 7 and key not in self.snaps:
                ev = self._evaluate(asset, open_ts, fetch_book=True)
                if ev.get("model_p_fav") is not None:
                    self._store_snap(key, ev)

            if key in self.traded_windows or key in self.quotes or key in self.filled:
                secs_left = open_ts + WINDOW_SEC - now
                if 0 < secs_left <= 60:
                    ev = self._book_for(asset, open_ts)
                    if ev:
                        self.research.log_orderbook(ev, phase="ramp")
                    spot = (ev.get("spot") if ev else None) or self.spot.price(asset)
                    if spot:
                        self.research.log_spot_tick(asset, open_ts, spot, secs_left)

    def _book_for(self, asset, open_ts):
        """Return a recent eval row with a depth ladder, reusing the book fetched
        by manage_quotes this loop when fresh, else fetching once. Avoids a second
        order-book call per loop during the final-60s ramp."""
        cached = self._book_cache.get((asset, open_ts))
        if cached and time.time() - cached[0] <= max(3.0, config.POLL_SECONDS * 1.5):
            return cached[1]
        ev = self._evaluate(asset, open_ts, fetch_book=True)
        return ev if ev.get("yes_book") else None

    def collect_trades(self):
        """Persist the public trade tape for every active window (current and
        previous slot), regardless of whether we quote it. Deduped by trade_id;
        each asset polled at most every TRADES_POLL_SEC."""
        if not self.research.ok:
            return
        now = time.time()
        cur = now_window()
        for open_ts in (cur, cur - WINDOW_SEC):
            if now > open_ts + WINDOW_SEC + 90:   # window fully settled; stop tape
                continue
            for asset in self.assets:
                key = (asset, open_ts)
                if now - self._last_trade_poll.get(key, 0.0) < TRADES_POLL_SEC:
                    continue
                ticker = self.find_ticker(asset, open_ts)
                if not ticker:
                    continue
                self._last_trade_poll[key] = now
                min_ts = self._trade_cursor.get(key)
                try:
                    r = self.client.get_trades(
                        ticker, min_ts=(min_ts - 2) if min_ts else None, limit=1000)
                except Exception:
                    continue
                trades = r.get("trades", [])
                if not trades:
                    continue
                max_ts = self.research.log_trades(asset, open_ts, ticker, trades)
                if max_ts:
                    self._trade_cursor[key] = max_ts

    def _store_snap(self, key, ev):
        self.snaps[key] = {
            "ticker": ev.get("ticker"),
            "strike": ev.get("strike"),
            "spot": ev.get("spot"),
            "sigma": ev.get("sigma_long"),
            "sigma_short": ev.get("sigma_short"),
            "vol_ratio": ev.get("vol_ratio"),
            "margin_eff": ev.get("margin_eff"),
            "secs_left": ev.get("secs_left"),
            "fav_side": ev.get("fav_side"),
            "model_p_fav": ev.get("model_p_fav"),
            "prem_at_snap": ev.get("prem"),
            "yes_ask_at_snap": ev.get("yes_ask"),
            "no_ask_at_snap": ev.get("no_ask"),
            "ls_side_at_snap": ev.get("ls_side"),
            "ls_price_at_snap": ev.get("ls_price"),
            "floor_strike": self.official_strikes.get(key),
            "recorded": False,
        }

    def settle_windows(self):
        """Write the supervised label (window_outcomes) for every closed window.

        Reads the *persisted* window_timeline snapshot -- so it survives restarts,
        unlike the old in-memory ``snaps`` path that silently stopped after the
        first window -- and joins the settle result: our own ledger for windows we
        traded (no API call), the exchange otherwise. Throttled and capped so the
        result lookups never stall the trade loop; fully defensive so any single
        failure skips one window rather than breaking the loop."""
        if not self.research.ok:
            return
        now = time.time()
        if now - self._last_outcome_sweep < OUTCOME_SWEEP_SEC:
            return
        self._last_outcome_sweep = now
        lo = int(now_window() - OUTCOME_LOOKBACK_SEC)
        hi = int(now - WINDOW_SEC - 20)          # only windows that have closed
        for asset, open_ts in self.research.windows_needing_outcomes(
                lo, hi, limit=OUTCOME_SWEEP_MAX):
            try:
                snap = self.research.timeline_snapshot(asset, open_ts)
                if not snap:
                    continue
                result, traded = self._ledger_result(asset, open_ts)
                if result is None:
                    if traded:
                        continue              # ours, but ledger not settled yet
                    ticker = snap.get("ticker")
                    if not ticker:
                        continue
                    result = self.client.get_market(ticker)["market"].get("result")
                if result not in ("yes", "no"):
                    continue
                row = build_outcome_row(
                    asset, open_ts, snap, result, traded, now,
                    self.official_strikes.get((asset, open_ts)))
                if row:
                    self.research.record_outcome(row)
            except Exception:
                continue

    def _ledger_result(self, asset, open_ts):
        """Settle result for a window from our own ledger, no API call.

        Returns ``(result, traded)``: ``result`` in {'yes','no'} once the position
        has settled, else ``None``; ``traded`` True if we hold a fill for it. The
        favorite is the side we bought; a settle price >= 0.5 means it won."""
        buy = self.con.execute(
            "SELECT side FROM trades WHERE mode = ? AND action = 'buy' "
            "AND asset = ? AND window_open_ts = ? LIMIT 1",
            (self.mode, asset, int(open_ts))).fetchone()
        if not buy:
            return None, False
        fav = buy["side"]
        st = self.con.execute(
            "SELECT price FROM trades WHERE mode = ? AND action = 'settle' "
            "AND asset = ? AND window_open_ts = ? LIMIT 1",
            (self.mode, asset, int(open_ts))).fetchone()
        if not st:
            return None, True
        won = st["price"] >= 0.5
        return (fav if won else ("no" if fav == "yes" else "yes")), True

    # ---------- startup recovery ----------
    def rehydrate_open_positions(self, lookback_sec=6 * 3600):
        """Reload filled-but-unsettled positions from the ledger on startup.

        ``self.filled`` lives only in memory, so a restart (deploy, crash,
        watchdog) between a fill and its settlement would otherwise orphan the
        position: it never settles and shows as a perpetually "open position"
        on the dashboard. On boot we rebuild it from the trades table for
        recent windows so ``settle()`` can finish them normally."""
        cutoff = now_window() - lookback_sec
        try:
            buys = self.con.execute(
                "SELECT ticker, window_open_ts, asset, side, qty, price, fees "
                "FROM trades WHERE mode=? AND action='buy' AND window_open_ts>=?",
                (self.mode, cutoff)).fetchall()
            settled = {(r["ticker"], r["window_open_ts"]) for r in self.con.execute(
                "SELECT ticker, window_open_ts FROM trades "
                "WHERE mode=? AND action='settle'", (self.mode,)).fetchall()}
        except Exception:
            return
        agg = {}
        for r in buys:
            wk = (r["ticker"], r["window_open_ts"])
            if wk in settled:
                continue
            a = agg.get(wk)
            if a is None:
                a = agg[wk] = {"ticker": r["ticker"], "asset": r["asset"],
                               "open_ts": r["window_open_ts"], "side": r["side"],
                               "qty": 0.0, "cost_dollars": 0.0, "fees": 0.0}
            a["qty"] += r["qty"]
            a["cost_dollars"] += r["qty"] * r["price"]
            a["fees"] += r["fees"] or 0.0
        n = 0
        for a in agg.values():
            if a["qty"] <= 0:
                continue
            key = (a["asset"], a["open_ts"])
            self.filled[key] = {"ticker": a["ticker"], "asset": a["asset"],
                                "open_ts": a["open_ts"], "side": a["side"],
                                "price": a["cost_dollars"] / a["qty"],
                                "qty": a["qty"], "fees": a["fees"]}
            self.traded_windows.add(key)
            n += 1
        if n:
            log_event(self.con, "info",
                      f"rehydrated {n} open position(s) from ledger for settlement")

    # ---------- housekeeping ----------
    def roll_day(self):
        today = datetime.date.today()
        if self.daily_date != today:
            self.daily_date = today
            self.daily_pnl = 0.0
            self.halted = False
        if self.daily_pnl < -config.MAX_DAILY_LOSS and not self.halted:
            self.halted = True
            for key in list(self.quotes):
                self.cancel_quote(key)
            log_event(self.con, "warn", f"DAILY LOSS HALT pnl={self.daily_pnl:.2f}")

    def record_equity(self):
        # NOTE: the Kalshi account balance is shared with other bots on this
        # account, so we do NOT store it here. Track this strategy's own
        # synthetic equity = start bankroll + lifetime realized pnl (from our
        # ledger), keeping the dashboard isolated to this strat.
        row = self.con.execute(
            "SELECT note FROM trades WHERE mode=? AND action='settle'", (self.mode,)
        ).fetchall()
        realized = 0.0
        for (note,) in row:
            if note and "pnl=" in note:
                try:
                    realized += float(note.split("=")[1])
                except ValueError:
                    pass
        bal = config.START_BANKROLL + realized
        self.bankroll = bal           # used by Kelly sizing
        open_risk = sum(p["qty"] * p["price"] for p in self.filled.values()
                        if not p.get("done"))
        self.con.execute(
            "INSERT OR REPLACE INTO equity (ts, mode, balance, open_risk, realized_pnl) "
            "VALUES (?,?,?,?,?)",
            (int(time.time()) // 60 * 60, self.mode, bal, open_risk, self.daily_pnl))
        self.con.commit()

    def prune(self):
        cutoff = time.time() - 6 * WINDOW_SEC
        for d in (self.strikes, self.official_strikes, self.tickers):
            for k in [k for k in d if k[1] < cutoff]:
                d.pop(k, None)
        for k in [k for k, p in self.filled.items()
                  if p.get("done") and p["open_ts"] < cutoff]:
            self.filled.pop(k, None)
        for k in [k for k in self.snaps if k[1] < cutoff]:
            self.snaps.pop(k, None)
        for k in [k for k in self.traded_windows if k[1] < cutoff]:
            self.traded_windows.discard(k)
        for d in (self._book_cache, self._trade_cursor, self._last_trade_poll):
            for k in [k for k in d if k[1] < cutoff]:
                d.pop(k, None)
        for k in [k for k in self.quotes if k[1] < now_window()]:
            self.cancel_quote(k, reason="window_end")
        self.research.prune_memory(int(time.time()) - 6 * WINDOW_SEC)
        self.research.prune_db()

    # ---------- main loop ----------
    def run(self):
        ab_note = (f" AB_TEST=on 3-way (B sticky {ab.AB_REPLACE_THRESHOLD:.2f}/"
                   f"{ab.AB_MIN_IN_BOOK_SEC:.0f}s, C fast {ab.AB_FAST_REPLACE_THRESHOLD:.2f}/"
                   f"{ab.AB_FAST_MIN_IN_BOOK_SEC:.0f}s)" if ab.AB_TEST else "")
        log_event(self.con, "info",
                  f"engine start mode={self.mode} assets={self.assets} "
                  f"margin={config.MARGIN} ls=[{config.MIN_LS_PRICE},{config.MAX_LS_PRICE}] "
                  f"qty={config.CONTRACTS_PER_QUOTE}{ab_note}")
        self.rehydrate_open_positions()
        self.spot.start()
        time.sleep(5)
        last_equity = 0.0
        last_beat = time.time()
        while True:
            t0 = time.time()
            try:
                self.roll_day()
                self.manage_quotes()
                self.check_fills()
                self.track_windows()
                self.collect_trades()
                self.settle()
                self.settle_windows()
                self.prune()
                self.research.maybe_flush()
                if time.time() - last_equity > 60:
                    self.record_equity()
                    last_equity = time.time()
                if time.time() - last_beat > 300:
                    qd = {f"{k[0]}": f"{q.ls_side}@{q.price:.2f}"
                          for k, q in self.quotes.items()}
                    log_event(self.con, "debug",
                              f"heartbeat quotes={qd} diag={dict(self.diag)} "
                              f"open={sum(1 for p in self.filled.values() if not p.get('done'))} "
                              f"day_pnl={self.daily_pnl:.2f}")
                    last_beat = time.time()
            except Exception:
                log_event(self.con, "error", traceback.format_exc()[-500:])
            time.sleep(max(0.2, config.POLL_SECONDS - (time.time() - t0)))
