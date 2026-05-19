"""One-off backfill: pull a single wallet's full trade history.

The original version paginated /trades?user=W with offset and stopped
around 1000 rows because the Data API's deep-pagination 400 fired early.
Wallets with 3000+ trades silently lost most of their history.

This version mirrors analyze_wallet.py's trick: discover every
conditionId the wallet has touched (via /activity REDEEM/MERGE/CONVERSION
+ /activity TRADE up to its 3500 cap + /positions), then fetch trades
per-market with /trades?user=W&market=CID. The per-market filter narrows
each call enough that the cap doesn't fire, so we get every trade.

Reuses ingest.upsert (synthetic id, ON CONFLICT, get_market_meta
enrichment) and ingest.write_scores ($30k floor + settlement filter
applied as usual).

Run with: python backfill_wallet.py 0xWALLETADDR
"""
import os
import sys
import time

import requests
import psycopg2
from dotenv import load_dotenv

import ingest
import score

load_dotenv()
DB_URL = os.environ["DATABASE_URL"]

ACTIVITY_URL  = "https://data-api.polymarket.com/activity"
TRADES_URL    = "https://data-api.polymarket.com/trades"
POSITIONS_URL = "https://data-api.polymarket.com/positions"

PAGE = 500
MAX_OFFSET = 100_000


def _paginate(url, params):
    """Offset-paginate a Data API endpoint, stopping cleanly on the 400
    deep-pagination cap (same handling as ingest.fetch_trades)."""
    out, offset = [], 0
    while offset < MAX_OFFSET:
        p = dict(params, limit=PAGE, offset=offset)
        r = requests.get(url, params=p, timeout=30)
        try:
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 400:
                break
            raise
        batch = r.json() or []
        if not batch:
            break
        out.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return out


def discover_condition_ids(address):
    """Every conditionId the wallet has touched, by union of:
      - /activity REDEEM / MERGE / CONVERSION (small, paginates cleanly)
      - /activity TRADE (caps at 3500 but the recent 3500 cover most)
      - /positions (active + redeemable)"""
    cids = set()
    for etype in ("REDEEM", "MERGE", "CONVERSION", "TRADE"):
        events = _paginate(ACTIVITY_URL, {"user": address, "type": etype})
        for e in events:
            if e.get("conditionId"):
                cids.add(e["conditionId"])
        print(f"  /activity?type={etype:<11} → {len(events)} events, cumulative cids: {len(cids)}")
    positions = _paginate(POSITIONS_URL, {"user": address})
    for p in positions:
        if p.get("conditionId"):
            cids.add(p["conditionId"])
    print(f"  /positions             → {len(positions)} entries, cumulative cids: {len(cids)}")
    return cids


def fetch_market_trades(address, condition_id):
    """All trades for this wallet on this market. The market= filter
    narrows enough that the deep-pagination cap doesn't fire."""
    return _paginate(TRADES_URL, {"user": address, "market": condition_id})


def known_condition_ids(conn, cids):
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
        print(f"Discovering condition_ids for {address}…")
        cids = discover_condition_ids(address)
        print(f"Total distinct markets: {len(cids)}\n")

        print(f"Fetching per-market trades ({len(cids)} markets)…")
        all_trades = []
        t0 = time.time()
        for i, cid in enumerate(sorted(cids), 1):
            all_trades.extend(fetch_market_trades(address, cid))
            if i % 25 == 0 or i == len(cids):
                print(f"  {i:>3}/{len(cids)} fetched  "
                      f"({len(all_trades)} trades, {time.time()-t0:.1f}s)")
        print(f"Fetched {len(all_trades)} trades total")

        prior_cids = known_condition_ids(conn, cids)
        new_trades = ingest.upsert(conn, all_trades)
        new_cids = {t["conditionId"] for t in new_trades} - prior_cids

        n_scored = ingest.write_scores(conn, new_trades)

        print()
        print(f"  total fetched:           {len(all_trades)}")
        print(f"  new inserts:             {len(new_trades)}")
        print(f"  new condition_ids:       {len(new_cids)}")
        print(f"  newly scored rows:       {n_scored} "
              f"(>= ${score.MIN_NOTIONAL:,.0f}, pre-resolution)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
