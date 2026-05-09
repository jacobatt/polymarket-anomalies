"""One-off historical backfill of score columns for existing >= $50k trades.

Run after the migration that adds score / notional_score / counter_trend
columns. Loads the full scoreable history via score.score_recent (which
filters to notional >= $50k internally) and UPDATEs each row by id.

Sub-$50k rows are intentionally left with NULL score columns. The dashboard
filters use SQL three-valued logic, so NULL rows don't appear when the user
filters by score >= N.

Coverage: score.score_recent has a built-in 30-day window. If the trades
table has rows older than 30 days they won't be backfilled here. Rerun with
score.score_window(0) if you need full-history coverage.

Run with: python backfill_scores.py
"""
import os

import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

import score

load_dotenv()
DB_URL = os.environ["DATABASE_URL"]


def main():
    conn = psycopg2.connect(DB_URL)
    try:
        # Score the largest window score_recent will give us (30 days * 24h).
        # The `hours` arg trims the output, not the load — pass enough to
        # cover the full LOOKBACK_DAYS.
        df = score.score_recent(hours=score.LOOKBACK_DAYS * 24)
        if df.empty:
            print("No scoreable trades found (>= $50k in last 30 days). Nothing to update.")
            return
        print(f"Scoring backfill: {len(df)} rows to update")

        rows = [
            (
                float(r["score"]),
                float(r["notional_score"]),
                bool(r["counter_trend"]),
                r["id"],
            )
            for _, r in df.iterrows()
        ]
        cur = conn.cursor()
        execute_values(
            cur,
            """
            UPDATE trades AS t SET
                score          = v.score,
                notional_score = v.notional_score,
                counter_trend  = v.counter_trend
            FROM (VALUES %s) AS v(score, notional_score, counter_trend, id)
            WHERE t.id = v.id
            """,
            rows,
            page_size=500,
        )
        conn.commit()
        cur.close()
        print(f"Updated {len(rows)} rows")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
