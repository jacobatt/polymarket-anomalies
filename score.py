"""Anomaly scoring over the trades table.

Only materially-sized trades (>= $50k notional) make it into the feed at all.
Each surviving trade gets a score combining:
  - notional_score: log10(notional / 50k), so $50k=0, $500k=1, $5M=2
  - counter_trend:  buy direction opposite to recent price drift, +3

Add new signals here (concentration, wallet age, market share) and they will
flow straight into the dashboard.
"""
import os
import pandas as pd
import numpy as np
import psycopg2
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.environ["DATABASE_URL"]

LOOKBACK_DAYS = 30
COUNTER_TREND_LOOKBACK = "1h"
MIN_NOTIONAL = 50_000.0  # hard filter - trades below this never enter the feed
SCORE_WEIGHT_SIZE = 1.0
SCORE_WEIGHT_COUNTER_TREND = 3.0


def load_recent_trades() -> pd.DataFrame:
    """Pull the last N days of >= $50k trades."""
    conn = psycopg2.connect(DB_URL)
    try:
        df = pd.read_sql(
            f"""
            SELECT id, timestamp, condition_id, proxy_wallet, side, size, price,
                   notional, title, slug, outcome
            FROM trades
            WHERE timestamp > EXTRACT(EPOCH FROM NOW() - INTERVAL '{LOOKBACK_DAYS} days')
              AND notional >= {MIN_NOTIONAL}
            ORDER BY condition_id, timestamp
            """,
            conn,
        )
    finally:
        conn.close()
    if not df.empty:
        df["dt"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).astype(
            "datetime64[ns, UTC]"
        )
    return df


def add_counter_trend(df: pd.DataFrame) -> pd.DataFrame:
    """Compare each trade's direction to the price drift over the prior hour."""
    out = []
    for _, g in df.groupby("condition_id", sort=False):
        g = g.sort_values("dt").copy()
        prior = g[["dt", "price"]].copy()
        prior["dt_lookup"] = prior["dt"] + pd.Timedelta(COUNTER_TREND_LOOKBACK)
        merged = pd.merge_asof(
            g.sort_values("dt"),
            prior.sort_values("dt_lookup")[["dt_lookup", "price"]].rename(
                columns={"price": "price_1h_ago"}
            ),
            left_on="dt",
            right_on="dt_lookup",
            direction="backward",
        )
        out.append(merged)
    df = pd.concat(out, ignore_index=True)
    df["trend"] = np.sign(df["price"] - df["price_1h_ago"]).fillna(0)
    df["dir"] = df["side"].map({"BUY": 1, "SELL": -1}).fillna(0)
    df["counter_trend"] = (df["trend"] != 0) & (df["trend"] != df["dir"])
    return df


def _load_window(since_ts: int) -> pd.DataFrame:
    """Load >= $50k trades from `since_ts - 1h` onward.

    The 1h buffer gives counter_trend the price-history context it needs
    for any market touched in the new window. Trades from before the buffer
    aren't loaded, since add_counter_trend only references same-market
    prior prices and 1h is the lookback window.
    """
    lookback_buffer = 3600  # seconds — matches COUNTER_TREND_LOOKBACK
    conn = psycopg2.connect(DB_URL)
    try:
        df = pd.read_sql(
            f"""
            SELECT id, timestamp, condition_id, proxy_wallet, side, size, price,
                   notional, title, slug, outcome
            FROM trades
            WHERE timestamp >= {int(since_ts) - lookback_buffer}
              AND notional >= {MIN_NOTIONAL}
            ORDER BY condition_id, timestamp
            """,
            conn,
        )
    finally:
        conn.close()
    if not df.empty:
        df["dt"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).astype(
            "datetime64[ns, UTC]"
        )
    return df


def score_window(since_ts: int) -> pd.DataFrame:
    """Score >= $50k trades with `timestamp >= since_ts`, returning their scores.

    Loads the window plus 1h of per-market prior context for counter_trend,
    runs the exact same math as score_recent, and returns only rows newer
    than since_ts. Used by ingest.py to score newly-inserted rows by id
    without re-scoring history.
    """
    df = _load_window(since_ts)
    if df.empty:
        return df
    df = add_counter_trend(df)
    df["notional_score"] = np.log10(df["notional"] / MIN_NOTIONAL)
    df["score"] = (
        df["notional_score"] * SCORE_WEIGHT_SIZE
        + df["counter_trend"].astype(int) * SCORE_WEIGHT_COUNTER_TREND
    )
    return df[df["timestamp"] >= int(since_ts)].reset_index(drop=True)


def score_recent(hours: int = 24) -> pd.DataFrame:
    """Return >= $50k trades from the last `hours` hours with scores attached."""
    df = load_recent_trades()
    if df.empty:
        return df
    df = add_counter_trend(df)
    df["notional_score"] = np.log10(df["notional"] / MIN_NOTIONAL)
    df["score"] = (
        df["notional_score"] * SCORE_WEIGHT_SIZE
        + df["counter_trend"].astype(int) * SCORE_WEIGHT_COUNTER_TREND
    )
    cutoff = df["dt"].max() - pd.Timedelta(hours=hours)
    return df[df["dt"] >= cutoff].sort_values("score", ascending=False).reset_index(drop=True)


if __name__ == "__main__":
    out = score_recent(24)
    print(f"Scored {len(out)} trades >= ${MIN_NOTIONAL:,.0f} from the last 24 hours")
    print(out[["dt", "title", "side", "notional", "notional_score", "counter_trend", "score"]].head(20))
