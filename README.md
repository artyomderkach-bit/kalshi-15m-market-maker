# Kalshi 15-minute Crypto Maker

A trading bot for Kalshi's 15-minute crypto up/down markets (BTC, ETH, SOL, XRP,
DOGE). It runs a single market-making strategy against a fair-value model, with a
full offline backtest pipeline behind it and a live dashboard in front.

> **This is the public version of the project, with the proprietary parts
> removed.** The fair-value model here uses a simplified variance term, the
> strategy parameters are illustrative defaults, and the specific live trading
> results are not included. The architecture, engine, backtest harness, and
> tooling are the real thing; the edge itself is not in this repo.

The project started as a research question — *is there a durable edge in these
markets?* — and the honest answer shaped everything else. I tested a fair number
of strategies on real bid/ask candle data across tens of thousands of windows.
Almost every edge that looked real in-sample decayed out-of-sample within a few
weeks as the market makers on the other side sharpened up. So rather than chase a
backtest curve, the bot ships in **paper mode** running the one strategy that is
roughly break-even under pessimistic fill assumptions, and measures its true
economics live — where requote latency (a couple of seconds in production vs a
full minute in the candle backtest) is the variable the backtest can't resolve.

This is not a promise of alpha.

## The strategy

Per asset and per 15-minute window, the engine looks at the **longshot** side of
the book (the cheap, unlikely outcome). When its ask sits above the model's fair
value by a margin, the engine rests a post-only offer at that ask — which is
equivalently a maker *buy of the favorite* at `1 − longshot_price` ($0.80–0.99
per contract). At most one fill per asset per window.

The fair-value model (`bot/model.py`) prices `P(up)` for a settlement defined by
the exchange's reference index, scaling realized 1-minute volatility by the time
left in the window. It is deliberately not Black-Scholes — these contracts have
their own settlement mechanics — and the same function is imported by both the
live engine and the backtests so they can never drift apart.

> Note: the public version of the model here uses a simplified diffusion
> variance. The production variance term is omitted.

## Architecture

Three pieces that talk only through SQLite files in `data/` (gitignored):

```
bot/        production engine — spot feed, fair-value model, paper/live execution
backtest/   offline research — data fetchers, simulators, calibration
app.py      Streamlit dashboard (reads the ledger read-only)
scripts/    ops — watchdog, calibration report, deploy helpers
deploy.sh   rsync + systemd deploy to a small VPS
```

- **`bot/engine.py`** runs a ~2s loop: evaluate the book against the model,
  place / replace / cancel quotes, detect fills, settle PnL, and record
  microstructure. `_evaluate()` is the single source of truth — every other step
  consumes its output.
- **Paper vs live fill detection** is the key fork. In paper mode the engine
  reads the public trades tape and treats a print going *through* its resting
  level as a fill. In live mode it places real post-only orders and polls their
  fill count. Paper is the honest lower bound; it never lets a touch count as a
  fill.
- **Two databases.** `data/bot.db` is the ledger (`trades`, `positions`,
  `equity`, `events`) — the bot writes, the dashboard reads. `data/spot_history.db`
  is raw microstructure (order-book snapshots, trade tape, quote lifecycle, spot
  ticks) kept for offline model work, with every high-volume table
  retention-bounded so the file plateaus in size over weeks.
- **Restart safety.** Open (filled-but-unsettled) positions are rebuilt from the
  ledger on boot, so a crash or redeploy between a fill and its settlement never
  orphans a position.

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # set KALSHI_API_KEY_ID and your key path; keep PAPER_MODE=true

.venv/bin/python -m bot.run            # trading engine (paper by default)
.venv/bin/streamlit run app.py         # dashboard on :8501
.venv/bin/python -m pytest tests/ -q   # tests
```

`PAPER_MODE=true` is the default and you should leave it there until paper
results actually justify going live. All strategy and risk parameters live in
`bot/config.py` and are overridable from `.env`.

## Data pipeline

Kalshi only retains 15-minute candles for ~9 weeks, so the fetchers are resumable
and meant to run periodically to accumulate history locally.

```bash
.venv/bin/python backtest/fetch_candles.py     # Kalshi 1-min bid/ask candles
.venv/bin/python backtest/fetch_spot.py        # Coinbase 1-min spot
.venv/bin/python backtest/build_settlements.py
```

## Conventions

- Prices are handled in **dollars** (0.0–1.0) internally; the book parsers accept
  either cents or dollar-denominated payloads and normalize.
- Kalshi fees round **up** to the next cent per execution; the backtests model
  this exactly (`bot/model.py::fee`).
- New strategy parameters go in `bot/config.py` with an `os.getenv` override.

## Stack

Python · SQLite · Streamlit · Plotly · pandas · RSA-PSS-signed REST · systemd
