"""Pull new Polymarket trades from the official Data API into Postgres.

Run locally with `python ingest.py`, or on a schedule via the GitHub Action
in .github/workflows/ingest.yml.
"""
import os
import time
from functools import lru_cache

import requests
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

import score

load_dotenv()

DB_URL = os.environ["DATABASE_URL"]
DATA_API_URL = os.environ.get("POLYMARKET_API_URL", "https://data-api.polymarket.com/trades")
GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"

PAGE_SIZE = 1000                  # API caps limit at 10000; smaller pages are politer
MAX_OFFSET = 100_000              # safety stop for cold-start runs
BOOTSTRAP_LOOKBACK_DAYS = 7       # how far back to seed when the table is empty
OVERLAP_SECONDS = 300             # re-scan the last 5 min so newly-arrived trades
                                  # that shifted offsets mid-page aren't missed

# Discord alerts moved out of this cron and into the Vercel worker at
# /api/alerts/run, driven by the alert_rules table. See migration.md
# § "Alert worker" in the polyanomalies repo. The legacy ">= $100k"
# behavior can be reproduced by adding a rule with min_notional=100000.


def make_id(t: dict) -> str:
    return f"{t['transactionHash']}_{t['proxyWallet']}_{t['asset']}"


@lru_cache(maxsize=1024)
def get_market_meta(condition_id: str):
    """Fetch (category, end_date_iso) for a market from Gamma. Cached per
    condition for the run. end_date is the resolution timestamp; trades
    after it are settlement noise, not predictions, and score.py drops
    them from feed eligibility.

    Gamma's `/markets/{id}` path expects a numeric internal id; using a
    conditionId hex string there hits an undefined route that hangs the
    socket past any read timeout. The query-param form is the documented
    lookup-by-conditionId path and returns a list (we always limit=1)."""
    try:
        r = requests.get(
            GAMMA_API_URL,
            params={"conditionIds": condition_id, "limit": 1},
            timeout=10,
        )
        if r.ok:
            data = r.json() or []
            if data:
                m = data[0]
                return (m.get("category"), m.get("endDate"))
    except requests.exceptions.RequestException:
        pass
    return (None, None)


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


def upsert(conn, trades):
    """Insert trades, skipping dupes. Returns the list of dicts actually inserted."""
    if not trades:
        return []
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
            *get_market_meta(t["conditionId"]),  # (category, market_end_date)
        )
        for t in trades
    ]
    cur = conn.cursor()
    returned = execute_values(
        cur,
        """
        INSERT INTO trades (
            id, transaction_hash, proxy_wallet, asset, condition_id,
            side, size, price, timestamp,
            title, slug, outcome, outcome_index, name, pseudonym,
            category, market_end_date
        )
        VALUES %s
        ON CONFLICT (id) DO NOTHING
        RETURNING id
        """,
        rows,
        page_size=500,
        fetch=True,
    )
    conn.commit()
    cur.close()
    new_ids = {r[0] for r in returned}
    return [t for t in trades if make_id(t) in new_ids]


def write_scores(conn, new_trades):
    """Score the just-inserted trades and UPDATE their score columns by id."""
    if not new_trades:
        return 0
    new_ids = {make_id(t) for t in new_trades}
    min_ts = min(int(t["timestamp"]) for t in new_trades)
    scored = score.score_window(min_ts)
    if scored.empty:
        return 0
    scored = scored[scored["id"].isin(new_ids)]
    if scored.empty:
        return 0
    rows = [
        (
            float(r["score"]),
            float(r["notional_score"]),
            bool(r["counter_trend"]),
            r["id"],
        )
        for _, r in scored.iterrows()
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
        new_trades = upsert(conn, trades)
        print(f"Upserted {len(new_trades)} new rows. Latest ts is now {latest_timestamp(conn)}")
        n_scored = write_scores(conn, new_trades)
        if n_scored:
            print(f"Scored {n_scored} new rows (>= ${score.MIN_NOTIONAL:,.0f})")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
