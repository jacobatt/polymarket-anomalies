"""One-off historical backfill of score columns for existing >= $30k trades.

Three passes:
  1. Backfill `market_end_date` for any condition_id that's still NULL.
     Uses ingest.get_market_meta which is lru_cached, so each market is
     hit once even though many trade rows share it.
  2. Clear scores on settlement trades (timestamp at or past resolution).
     These got scored under the old logic and need to disappear from the
     feed — set score, notional_score back to NULL.
  3. Re-score using score.score_recent (which now filters out settlement
     trades at the SQL level) and bulk-UPDATE survivors.

Sub-$30k rows stay NULL throughout — same partial-index semantics. After
running, `SELECT COUNT(*) FROM trades WHERE score IS NOT NULL` should
drop relative to before.

Run with: python backfill_scores.py
"""
import os

import requests
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

import score
from ingest import GAMMA_API_URL

load_dotenv()
DB_URL = os.environ["DATABASE_URL"]

GAMMA_BATCH = 100


def _fetch_end_dates_batch(condition_ids):
    """Query Gamma for up to ~100 markets at once via ?condition_ids=A,B,...
    Returns {conditionId: end_date_iso} for whichever markets came back.
    Failures (timeout, 5xx, malformed JSON) log + return {} so one bad
    batch doesn't tank the run."""
    try:
        r = requests.get(
            GAMMA_API_URL,
            params={"conditionIds": ",".join(condition_ids), "limit": len(condition_ids)},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json() or []
        out = {}
        for m in data:
            cid = m.get("conditionId")
            if cid:
                out[cid] = m.get("endDate")
        return out
    except (requests.exceptions.RequestException, ValueError) as e:
        print(f"  Gamma batch failed ({len(condition_ids)} cids): {e}")
        return {}


def backfill_end_dates(conn) -> int:
    """For every distinct condition_id whose rows have NULL market_end_date,
    batch-fetch from Gamma and bulk-UPDATE per batch. Returns rows touched."""
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT condition_id FROM trades WHERE market_end_date IS NULL"
    )
    cids = [r[0] for r in cur.fetchall()]
    cur.close()
    if not cids:
        print("All markets already have end_date — skipping enrichment")
        return 0

    n_batches = (len(cids) + GAMMA_BATCH - 1) // GAMMA_BATCH
    print(
        f"Backfilling end_date for {len(cids)} markets via Gamma "
        f"({n_batches} batches of {GAMMA_BATCH})…"
    )

    touched = 0
    seen_with_end = 0
    for b in range(n_batches):
        batch = cids[b * GAMMA_BATCH : (b + 1) * GAMMA_BATCH]
        meta = _fetch_end_dates_batch(batch)
        rows = [(cid, ed) for cid, ed in meta.items() if ed]
        if rows:
            cur = conn.cursor()
            execute_values(
                cur,
                """
                UPDATE trades AS t
                SET market_end_date = v.end_date::timestamptz
                FROM (VALUES %s) AS v(condition_id, end_date)
                WHERE t.condition_id = v.condition_id
                """,
                rows,
            )
            touched += cur.rowcount
            cur.close()
            seen_with_end += len(rows)
        conn.commit()
        print(
            f"  batch {b + 1}/{n_batches}: "
            f"{len(rows)}/{len(batch)} markets enriched, "
            f"running total {seen_with_end} markets / {touched} rows"
        )
    print(
        f"end_date set on {seen_with_end}/{len(cids)} markets ({touched} rows)"
    )
    return touched


def clear_settlement_scores(conn) -> int:
    """NULL the score columns on trades whose timestamp is at or past their
    market's end_date. Returns rows cleared."""
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE trades
        SET score = NULL,
            notional_score = NULL,
            counter_trend = false
        WHERE score IS NOT NULL
          AND market_end_date IS NOT NULL
          AND timestamp >= EXTRACT(EPOCH FROM market_end_date)
        """
    )
    n = cur.rowcount
    conn.commit()
    cur.close()
    print(f"Cleared scores on {n} settlement trades")
    return n


def write_scores(conn) -> int:
    """Run score_recent over the LOOKBACK_DAYS window and bulk-UPDATE
    matching rows. Returns rows updated."""
    df = score.score_recent(hours=score.LOOKBACK_DAYS * 24)
    if df.empty:
        print("No scoreable trades found (>= $30k, pre-resolution)")
        return 0
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
    print(f"Scored {len(rows)} rows")
    return len(rows)


def main():
    conn = psycopg2.connect(DB_URL)
    try:
        backfill_end_dates(conn)
        clear_settlement_scores(conn)
        write_scores(conn)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM trades WHERE score IS NOT NULL")
        (n,) = cur.fetchone()
        cur.close()
        print(f"\nFinal scored-row count: {n}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
