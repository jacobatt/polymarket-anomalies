"""Streamlit dashboard for Polymarket trade anomalies.

Run locally:  streamlit run app.py
Deploy:       https://share.streamlit.io  (point at app.py, add DATABASE_URL secret)
"""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go

from score import score_recent

st.set_page_config(page_title="Polymarket anomaly monitor", layout="wide")

# ---------- header
st.title("Polymarket anomaly monitor")
window_hours = st.sidebar.selectbox("Lookback window", [6, 24, 72, 168], index=1)
score_threshold = st.sidebar.slider("Score threshold", 0.0, 5.0, 0.0, 0.25)


@st.cache_data(ttl=120, show_spinner="Scoring recent trades...")
def load(hours: int) -> pd.DataFrame:
    return score_recent(hours=hours)


df = load(window_hours)

if df.empty:
    st.info("No trades in the database yet. Run `python ingest.py` first.")
    st.stop()

flagged = df[df["score"] >= score_threshold]

# ---------- KPI strip
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Trades in feed", len(flagged))
c2.metric("Whale notional", f"${flagged['notional'].sum() / 1e6:.2f}M")
c3.metric("Trades >= $1M", int((flagged["notional"] >= 1_000_000).sum()))
c4.metric("Counter-trend trades", int(flagged["counter_trend"].sum()))
c5.metric("Markets touched", flagged["condition_id"].nunique())

st.divider()

# ---------- anomaly feed
st.subheader("Anomaly feed")
feed = flagged[
    ["dt", "title", "outcome", "proxy_wallet", "side", "notional",
     "notional_score", "counter_trend", "score"]
].head(100).copy()
feed["dt"] = feed["dt"].dt.strftime("%Y-%m-%d %H:%M")
feed["notional"] = feed["notional"].map(lambda v: f"${v:,.0f}")
feed["notional_score"] = feed["notional_score"].round(2)
feed["score"] = feed["score"].round(2)
st.dataframe(feed, use_container_width=True, hide_index=True)

st.divider()

# ---------- market drill-down
st.subheader("Market drill-down")
market_counts = flagged["condition_id"].value_counts()
if market_counts.empty:
    st.info("No flagged markets in this window.")
    st.stop()

# Map condition_id -> readable title for the selectbox
title_map = (
    df.dropna(subset=["title"])
      .drop_duplicates("condition_id")
      .set_index("condition_id")["title"]
      .to_dict()
)
markets = market_counts.index.tolist()
market = st.selectbox(
    "Pick a market",
    markets,
    format_func=lambda cid: title_map.get(cid, cid[:16] + "..."),
)
mdf = df[df["condition_id"] == market].sort_values("dt")

fig = go.Figure()
fig.add_trace(
    go.Scatter(
        x=mdf["dt"],
        y=mdf["price"],
        mode="lines",
        name="price",
        line=dict(color="#378ADD", width=1.5),
    )
)
flags_in_market = mdf[mdf["score"] >= score_threshold]
fig.add_trace(
    go.Scatter(
        x=flags_in_market["dt"],
        y=flags_in_market["price"],
        mode="markers",
        name="anomaly",
        marker=dict(
            size=10,
            color=flags_in_market["score"],
            colorscale="Reds",
            showscale=True,
            colorbar=dict(title="score"),
            line=dict(color="white", width=1),
        ),
        hovertemplate=(
            "<b>%{customdata[0]}</b><br>"
            "Price: %{y:.3f}<br>"
            "Notional: $%{customdata[1]:,.0f}<br>"
            "Log size: %{customdata[2]:.2f}<br>"
            "Score: %{customdata[3]:.2f}<extra></extra>"
        ),
        customdata=flags_in_market[["dt", "notional", "notional_score", "score"]].values,
    )
)
fig.update_layout(
    height=420,
    margin=dict(l=0, r=0, t=20, b=0),
    yaxis_title=f"{title_map.get(market, market)[:60]} - price",
    xaxis_title="",
    hovermode="closest",
)
st.plotly_chart(fig, use_container_width=True)

# ---------- top wallets
st.subheader("Top wallets by notional (this window)")
wallet_table = (
    df.groupby("proxy_wallet")
    .agg(
        total_notional=("notional", "sum"),
        trade_count=("id", "count"),
        markets_touched=("condition_id", "nunique"),
    )
    .sort_values("total_notional", ascending=False)
    .head(20)
    .reset_index()
)
wallet_table["total_notional"] = wallet_table["total_notional"].map(lambda v: f"${v:,.0f}")
st.dataframe(wallet_table, use_container_width=True, hide_index=True)
