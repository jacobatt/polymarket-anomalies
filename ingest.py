"""Pull new Polymarket trades from the official Data API into Postgres.

Run locally with `python ingest.py`, or on a schedule via the GitHub Action
in .github/workflows/ingest.yml.
"""
import os
import time
import requests
import psycopg2
from psycopg2.extras import execute_batch
from dotenv import load_dotenv

load_dotenv()

DB_URL = os.environ["DATABASE_URL"]
DATA_API_URL = os.environ.get("POLYMARKET_API_URL", "https://data-api.polymarket.com/trades")

PAGE_SIZE = 1000                  # API caps limit at 10000; smaller pages are politer
MAX_OFFSET = 100_000              # safety stop for cold-start runs
BOOTSTRAP_LOOKBACK_DAYS = 7       # how far back to seed when the table is empty
OVERLAP_SECONDS = 300             # re-scan the last 5 min so newly-arrived trades
                                  # that shifted offsets mid-page aren't missed


def make_id(t: dict) -> str:
    return f"{t['transactionHash']}_{t['proxyWallet']}_{t['asset']}"


def fetch_trades(since_ts: int):
    """Page newest-first through /trades and stop once we cross since_ts.

    The Data API has no timestamp filter, so we paginate by offset. We sort
    each page descending in Python rather than trusting the API's default
    order - if that order ever flips, early termination would silently drop
    rows. Dedup is handled by ON CONFLICT in upsert(), which lets us safely
    overlap windows between runs.
    """
    cutoff = max(0, since_ts - OVERLAP_SECONDS)
    out, offset = [], 0
    while offset < MAX_OFFSET:
        resp = requests.get(
            DATA_API_URL,
            params={"limit": PAGE_SIZE, "offset": offset},
            timeout=30,
        )
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 400:
                print(
                    f"Pagination limit hit at offset {offset}, "
                    f"returning partial results ({len(out)} trades so far)"
                )
                break
            raise
        batch = resp.json()
        if not batch:
            break
        batch.sort(key=lambda t: int(t["timestamp"]), reverse=True)
        # Stop only when the *whole page* is at or below cutoff. If any trade
        # on this page is newer, keep them all (we still want them) and let
        # the next page decide.
        if int(batch[0]["timestamp"]) <= cutoff:
            break
        out.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return [t for t in out if int(t["timestamp"]) > cutoff]


def latest_timestamp(conn) -> int:
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(MAX(timestamp), 0) FROM trades")
    ts = cur.fetchone()[0]
    cur.close()
    return int(ts)


def upsert(conn, trades) -> int:
    if not trades:
        return 0
    rows = [
        (
            make_id(t),
            t["transactionHash"],
            t["proxyWallet"],
            t["asset"],
            t["conditionId"],
            t["side"],
            float(t["size"]),
            float(t["price"]),
            int(t["timestamp"]),
            t.get("title"),
            t.get("slug"),
            t.get("outcome"),
            int(t["outcomeIndex"]) if t.get("outcomeIndex") is not None else None,
            t.get("name"),
            t.get("pseudonym"),
        )
        for t in trades
    ]
    cur = conn.cursor()
    execute_batch(
        cur,
        """
        INSERT INTO trades (
            id, transaction_hash, proxy_wallet, asset, condition_id,
            side, size, price, timestamp,
            title, slug, outcome, outcome_index, name, pseudonym
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        rows,
        page_size=500,
    )
    conn.commit()
    cur.close()
    return len(rows)


def main():
    conn = psycopg2.connect(DB_URL)
    try:
        since = latest_timestamp(conn)
        if since == 0:
            since = int(time.time()) - BOOTSTRAP_LOOKBACK_DAYS * 86400
            print(f"Cold start - bootstrapping with {BOOTSTRAP_LOOKBACK_DAYS} days of history")
        print(f"Fetching trades after Unix ts {since}")
        trades = fetch_trades(since)
        print(f"Fetched {len(trades)} trades from Data API")
        n = upsert(conn, trades)
        print(f"Upserted {n} rows. Latest ts is now {latest_timestamp(conn)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
