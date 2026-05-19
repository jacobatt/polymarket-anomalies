"""One-off backfill: pull a single wallet's full trade history.

ingest.py paginates the firehose and stops once it crosses the last seen
timestamp, so wallets that traded outside the recent window are sparse in
the table. This script narrows by `?user={address}`, which keeps each
request shallow and lets us walk a wallet back as far as the API exposes.

Reuses ingest.upsert (synthetic id, ON CONFLICT DO NOTHING, get_market_meta
enrichment) and ingest.write_scores (score.score_window over the new rows'
min timestamp, $30k floor, settlement filter applied as usual).

Run with: python backfill_wallet.py 0xWALLETADDR
"""
import os
import sys

import requests
import psycopg2
from dotenv import load_dotenv

import ingest
import score

load_dotenv()
DB_URL = os.environ["DATABASE_URL"]

DATA_URL = "https://data-api.polymarket.com/trades"
PAGE = 1000
MAX_OFFSET = 100_000  # safety stop; the API typically 400s long before this


def fetch_wallet_trades(address: str):
    """Page newest-first through /trades?user=... until exhausted or 400.

    Same graceful 400 handling as ingest.fetch_trades — the Data API caps
    deep pagination, and we return whatever we got rather than crashing."""
    out, offset = [], 0
    while offset < MAX_OFFSET:
        resp = requests.get(
            DATA_URL,
            params={"user": address, "limit": PAGE, "offset": offset},
            timeout=30,
        )
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 400:
                print(
                    f"  Pagination cap hit at offset {offset}, "
                    f"returning {len(out)} trades fetched so far"
                )
                break
            raise
        batch = resp.json()
        if not batch:
            break
        out.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return out


def known_condition_ids(conn, cids):
    """Which of the given condition_ids are already present in trades?"""
    if not cids:
        return set()
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT condition_id FROM trades WHERE condition_id = ANY(%s)",
        (list(cids),),
    )
    found = {r[0] for r in cur.fetchall()}
    cur.close()
    return found


def main():
    if len(sys.argv) != 2:
        print("usage: python backfill_wallet.py <wallet_address>")
        sys.exit(1)
    address = sys.argv[1]

    conn = psycopg2.connect(DB_URL)
    try:
        print(f"Fetching trades for {address}…")
        trades = fetch_wallet_trades(address)
        print(f"Fetched {len(trades)} trades from Data API")

        seen_cids = {t["conditionId"] for t in trades}
        prior_cids = known_condition_ids(conn, seen_cids)

        new_trades = ingest.upsert(conn, trades)
        new_cids = {t["conditionId"] for t in new_trades} - prior_cids

        n_scored = ingest.write_scores(conn, new_trades)

        print()
        print(f"  total fetched:           {len(trades)}")
        print(f"  new inserts:             {len(new_trades)}")
        print(f"  new condition_ids:       {len(new_cids)}")
        print(f"  newly scored rows:       {n_scored} "
              f"(>= ${score.MIN_NOTIONAL:,.0f}, pre-resolution)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
