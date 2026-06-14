"""Kalshi REST client with RSA-PSS auth (pattern reused from kalshi-ev-bot)."""
import base64
import datetime
import os
import threading
import time
from pathlib import Path
from typing import Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "")
PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key")
if not os.path.isabs(PRIVATE_KEY_PATH):
    PRIVATE_KEY_PATH = str(PROJECT_ROOT / PRIVATE_KEY_PATH)

_private_key = None
_lock = threading.Lock()


def _load_private_key():
    global _private_key
    if _private_key is None:
        with open(PRIVATE_KEY_PATH, "rb") as f:
            _private_key = serialization.load_pem_private_key(f.read(), password=None)
    return _private_key


def _auth_headers(method: str, path: str) -> dict:
    key = _load_private_key()
    timestamp = str(int(datetime.datetime.now().timestamp() * 1000))
    full_path = f"/trade-api/v2{path}" if not path.startswith("/trade-api") else path
    message = f"{timestamp}{method}{full_path.split('?')[0]}".encode()
    sig = key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json",
    }


def longshot_sell_v2_body(ticker: str, ls_side: str, ls_price: float, qty,
                          client_order_id: str) -> dict:
    """Build a V2 create-order body for our maker sell of the longshot side.

    Kalshi V2 uses a single YES book: ``bid`` = buy YES, ``ask`` = sell YES.
    Selling the longshot NO at ``ls_price`` is posted as ``bid`` YES at
    ``1 - ls_price`` (economically equivalent).
    """
    if ls_side == "yes":
        side, price = "ask", float(ls_price)
    elif ls_side == "no":
        side, price = "bid", 1.0 - float(ls_price)
    else:
        raise ValueError(f"invalid ls_side: {ls_side!r}")
    return {
        "ticker": ticker,
        "client_order_id": client_order_id,
        "side": side,
        "count": f"{float(qty):.2f}",
        "price": f"{price:.4f}",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": True,
        "cancel_order_on_pause": True,
    }


def parse_order_id(create_resp: dict) -> Optional[str]:
    """Order id from V2 (top-level) or legacy V1 (nested under ``order``)."""
    if not create_resp:
        return None
    oid = create_resp.get("order_id")
    if oid:
        return oid
    return (create_resp.get("order") or {}).get("order_id")


def parse_fill_count(order: dict) -> float:
    """Filled contracts from a GET-order payload or V2 create response."""
    if not order:
        return 0.0
    for key in ("fill_count_fp", "fill_count"):
        val = order.get(key)
        if val is not None:
            return float(val)
    taker = float(order.get("taker_fill_count", 0) or 0)
    maker = float(order.get("maker_fill_count", 0) or 0)
    return taker + maker


class KalshiClient:
    def __init__(self, min_interval: float = 0.12):
        self.session = requests.Session()
        self.min_interval = min_interval
        self._last_req = 0.0

    def _throttle(self):
        with _lock:
            wait = self._last_req + self.min_interval - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last_req = time.time()

    def request(self, method: str, path: str, params=None, json_body=None, signed=False, retries=4):
        url = API_BASE + path
        for attempt in range(retries):
            self._throttle()
            headers = _auth_headers(method, path) if signed else {}
            try:
                resp = self.session.request(method, url, params=params, json=json_body,
                                            headers=headers, timeout=15)
                if resp.status_code == 429:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return resp.json() if resp.content else {}
            except requests.RequestException:
                if attempt == retries - 1:
                    raise
                time.sleep(1.0 * (attempt + 1))
        raise RuntimeError("unreachable")

    # ---- market data (signed by default for higher rate limits) ----
    def get_markets(self, series_ticker, status=None, cursor=None, limit=1000,
                    min_close_ts=None, max_close_ts=None, signed=True):
        params = {"series_ticker": series_ticker, "limit": limit}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        if min_close_ts:
            params["min_close_ts"] = min_close_ts
        if max_close_ts:
            params["max_close_ts"] = max_close_ts
        return self.request("GET", "/markets", params=params, signed=signed)

    def get_market(self, ticker, signed=True):
        return self.request("GET", f"/markets/{ticker}", signed=signed)

    def get_candlesticks(self, series_ticker, ticker, start_ts, end_ts, period_interval=1,
                         signed=True):
        params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval}
        return self.request("GET", f"/series/{series_ticker}/markets/{ticker}/candlesticks",
                            params=params, signed=signed)

    def get_orderbook(self, ticker, depth=10, signed=True):
        return self.request("GET", f"/markets/{ticker}/orderbook",
                            params={"depth": depth}, signed=signed)

    def get_trades(self, ticker, min_ts=None, limit=100, signed=True):
        params = {"ticker": ticker, "limit": limit}
        if min_ts:
            params["min_ts"] = int(min_ts)
        return self.request("GET", "/markets/trades", params=params, signed=signed)

    # ---- portfolio (signed) ----
    def get_balance(self):
        return self.request("GET", "/portfolio/balance", signed=True)

    def get_positions(self, **params):
        return self.request("GET", "/portfolio/positions", params=params, signed=True)

    def get_fills(self, **params):
        return self.request("GET", "/portfolio/fills", params=params, signed=True)

    def get_orders(self, **params):
        return self.request("GET", "/portfolio/orders", params=params, signed=True)

    def get_order(self, order_id: str) -> dict:
        """Single order; GET /portfolio/orders/{id} still works for V2 placements."""
        return self.request("GET", f"/portfolio/orders/{order_id}", signed=True)

    def create_order(self, body: dict):
        """Place a resting longshot-seller order via V2 ``/portfolio/events/orders``."""
        return self.request("POST", "/portfolio/events/orders", json_body=body, signed=True)

    def create_longshot_sell(self, ticker: str, ls_side: str, ls_price: float, qty,
                             client_order_id: str):
        """Convenience wrapper: build V2 body + submit."""
        body = longshot_sell_v2_body(ticker, ls_side, ls_price, qty, client_order_id)
        return self.create_order(body)

    def cancel_order(self, order_id: str):
        return self.request("DELETE", f"/portfolio/events/orders/{order_id}", signed=True)
