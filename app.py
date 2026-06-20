"""Streamlit dashboard for the Kalshi 15-min crypto bot.

Read-only view of the trading ledger in data/bot.db: realized PnL, the equity
curve, per-asset breakdown, open positions, recent fills, model calibration, and
the event log. The bot writes the ledger; this dashboard only ever reads it.
"""
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from bot import config
from bot.db import connect

st.set_page_config(page_title="Kalshi 15m Crypto Bot", layout="wide")
MODE = "paper" if config.PAPER_MODE else "live"


def q(sql, params=()):
    """Run a read-only query; return an empty frame if the DB isn't ready yet."""
    try:
        con = connect(readonly=True)
        df = pd.read_sql_query(sql, con, params=params)
        con.close()
        return df
    except Exception:
        return pd.DataFrame()


st.title("Kalshi 15-min Crypto Bot")
st.caption(
    f"Longshot-seller maker · mode: **{MODE}** · "
    f"bankroll: ${config.START_BANKROLL:,.0f} · assets: {', '.join(config.ASSETS)}"
)

settled = q(
    "SELECT window_open_ts, asset, side, qty, avg_cost, fees, result, pnl "
    "FROM positions WHERE settled = 1 ORDER BY window_open_ts"
)
open_pos = q(
    "SELECT ticker, asset, side, qty, avg_cost, fees, window_open_ts "
    "FROM positions WHERE settled = 0 ORDER BY window_open_ts DESC"
)

# --- headline metrics -------------------------------------------------------
realized = float(settled["pnl"].sum()) if not settled.empty else 0.0
n_fills = int(len(settled))
wins = int((settled["pnl"] > 0).sum()) if not settled.empty else 0
win_rate = (wins / n_fills) if n_fills else 0.0
ret_pct = realized / config.START_BANKROLL * 100 if config.START_BANKROLL else 0.0

c1, c2, c3, c4 = st.columns(4)
c1.metric("Realized PnL", f"${realized:,.2f}", f"{ret_pct:+.1f}% on bankroll")
c2.metric("Settled fills", f"{n_fills:,}")
c3.metric("Win rate", f"{win_rate*100:.1f}%")
c4.metric("Open positions", f"{len(open_pos):,}")

# --- equity curve -----------------------------------------------------------
st.subheader("Cumulative PnL")
if settled.empty:
    st.info("No settled fills yet. The ledger fills in as the bot trades.")
else:
    curve = settled.copy()
    curve["cum_pnl"] = curve["pnl"].cumsum()
    curve["t"] = pd.to_datetime(curve["window_open_ts"], unit="s")
    fig = go.Figure(go.Scatter(x=curve["t"], y=curve["cum_pnl"], mode="lines"))
    fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0),
                      yaxis_title="cumulative PnL ($)", xaxis_title=None)
    st.plotly_chart(fig, use_container_width=True)

# --- per-asset + calibration ------------------------------------------------
left, right = st.columns(2)

with left:
    st.subheader("PnL by asset")
    if settled.empty:
        st.caption("—")
    else:
        by_asset = (settled.groupby("asset")
                    .agg(pnl=("pnl", "sum"), fills=("pnl", "size"))
                    .reset_index().sort_values("pnl"))
        fig = go.Figure(go.Bar(x=by_asset["asset"], y=by_asset["pnl"]))
        fig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0),
                          yaxis_title="PnL ($)")
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(by_asset, hide_index=True, use_container_width=True)

with right:
    st.subheader("Model calibration")
    # Each entry fill carries the model's fair probability; bucket it and compare
    # to the realized win rate. A well-calibrated model sits on the diagonal.
    calib = q(
        "SELECT t.model_p AS model_p, p.pnl AS pnl "
        "FROM trades t JOIN positions p ON t.ticker = p.ticker "
        "WHERE t.action IN ('buy','sell') AND p.settled = 1 "
        "AND t.model_p IS NOT NULL"
    )
    if calib.empty or len(calib) < 20:
        st.caption("Not enough settled fills with a model probability yet.")
    else:
        calib["win"] = (calib["pnl"] > 0).astype(int)
        calib["bucket"] = (calib["model_p"] * 10).clip(0, 9).astype(int) / 10 + 0.05
        grp = (calib.groupby("bucket")
               .agg(predicted=("model_p", "mean"),
                    realized=("win", "mean"), n=("win", "size"))
               .reset_index())
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode="lines",
                                 line=dict(dash="dash"), name="ideal"))
        fig.add_trace(go.Scatter(x=grp["predicted"], y=grp["realized"],
                                 mode="markers", marker=dict(size=grp["n"].clip(6, 28)),
                                 name="observed"))
        fig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0),
                          xaxis_title="model P(win)", yaxis_title="realized win rate",
                          xaxis_range=[0, 1], yaxis_range=[0, 1])
        st.plotly_chart(fig, use_container_width=True)

# --- open positions ---------------------------------------------------------
st.subheader("Open positions")
if open_pos.empty:
    st.caption("None.")
else:
    show = open_pos.copy()
    show["window"] = pd.to_datetime(show["window_open_ts"], unit="s")
    st.dataframe(show[["window", "asset", "ticker", "side", "qty", "avg_cost", "fees"]],
                 hide_index=True, use_container_width=True)

# --- recent fills -----------------------------------------------------------
st.subheader("Recent fills")
fills = q(
    "SELECT ts, asset, ticker, side, action, qty, price, fees, is_maker, model_p, edge "
    "FROM trades WHERE action IN ('buy','sell') ORDER BY ts DESC LIMIT 50"
)
if fills.empty:
    st.caption("No fills yet.")
else:
    fills["time"] = pd.to_datetime(fills["ts"], unit="s")
    fills["maker"] = fills["is_maker"].map({1: "maker", 0: "taker"})
    st.dataframe(
        fills[["time", "asset", "ticker", "side", "action", "qty", "price",
               "fees", "maker", "model_p", "edge"]],
        hide_index=True, use_container_width=True)

# --- event log --------------------------------------------------------------
st.subheader("Event log")
events = q("SELECT ts, level, msg FROM events ORDER BY ts DESC LIMIT 100")
if events.empty:
    st.caption("No events yet.")
else:
    events["time"] = pd.to_datetime(events["ts"], unit="s")
    st.dataframe(events[["time", "level", "msg"]], hide_index=True,
                 use_container_width=True)
