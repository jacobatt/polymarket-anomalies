"""One-off backfill: pull recent trades from Polymarket's top markets by volume.

ingest.py paginates the unfiltered /trades firehose and hits a 400 around the
4000-row offset, so it misses the longer-tail markets (elections, sports,
multi-month events) where whales actually play. This script narrows each
request by conditionId, which keeps every call shallow and bypasses the cap.

Run once with: python backfill_big.py
"""
import os
import requests
import psycopg2
from dotenv import load_dotenv

from ingest import upsert

load_dotenv()
DB_URL = os.environ["DATABASE_URL"]

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
DATA_URL = "https://data-api.polymarket.com/trades"
TOP_N = 50
PAGE = 1000


def top_markets(n: int):
    resp = requests.get(
        GAMMA_URL,
        params={
            "active": "true",
            "order": "volume",
            "ascending": "false",
            "limit": n,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def trades_for_market(condition_id: str):
    resp = requests.get(
        DATA_URL,
        params={"conditionIds": condition_id, "limit": PAGE},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def main():
    markets = top_markets(TOP_N)
    print(f"Pulled {len(markets)} markets from Gamma (active, by volume desc)\n")

    conn = psycopg2.connect(DB_URL)
    try:
        total = 0
        for i, m in enumerate(markets, 1):
            cid = m.get("conditionId")
            label = (m.get("question") or m.get("slug") or "(unknown)")[:50]
            if not cid:
                print(f"[{i:>2}/{len(markets)}] skip - no conditionId  | {label}")
                continue
            try:
                trades = trades_for_market(cid)
            except requests.exceptions.HTTPError as e:
                code = e.response.status_code if e.response is not None else "?"
                print(f"[{i:>2}/{len(markets)}] HTTP {code:<3}              | {label}")
                continue
            n = upsert(conn, trades)
            total += n
            print(f"[{i:>2}/{len(markets)}] {n:>4} trades            | {label}")
        print(f"\nDone. Attempted {total} upserts (dupes skipped via ON CONFLICT).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
