"""One-off analysis: per-market realized P&L for a wallet, computed from
cash flows rather than trusting Polymarket's /positions realizedPnl field.

WHY NOT JUST USE /positions.realizedPnl?
  - That field is *trading-only* P&L (gains from partial sells during the
    life of a position). It does NOT include settlement payouts.
  - Positions that have been fully redeemed are removed from /positions
    entirely, so any winning bet that was claimed disappears from the
    snapshot. The biggest wins (Russia ceasefires, Israel/Hezbollah, US
    strikes Iran) all live in this blind spot.

CANONICAL P&L PER MARKET
  realized = sells_usdc + redeems_usdc + merges_usdc + conversions_usdc - buys_usdc
  where every inflow comes from /activity?type={REDEEM,MERGE,CONVERSION}
  and trades come from /trades?user=W&market=CID (the per-market endpoint
  bypasses the deep-pagination cap that breaks user-wide fetches).

CLASSIFICATION
  - Closed market: has a REDEEM/MERGE/CONVERSION event, OR net shares ≈ 0
    (fully traded out before resolution).
  - Open market: still holding net shares and no redemption yet.

Run with: python analyze_wallet.py 0xWALLETADDR
"""
import os
import sys
import time
from collections import defaultdict

import requests
from dotenv import load_dotenv

load_dotenv()

ACTIVITY_URL  = "https://data-api.polymarket.com/activity"
TRADES_URL    = "https://data-api.polymarket.com/trades"
POSITIONS_URL = "https://data-api.polymarket.com/positions"

PAGE = 500
MAX_OFFSET = 100_000


def _paginate(url, params):
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


def fetch_activity(address, event_type):
    return _paginate(ACTIVITY_URL, {"user": address, "type": event_type})


def fetch_positions(address):
    return _paginate(POSITIONS_URL, {"user": address})


def fetch_market_trades(address, condition_id):
    """Per-market trades for a wallet. The `market=` filter narrows enough
    that the deep-pagination 400 doesn't kick in (typical market has <50
    trades for any one wallet)."""
    return _paginate(TRADES_URL, {"user": address, "market": condition_id})


# Topic buckets. First match wins.
TOPICS = [
    ("Israel/Hezbollah", ["hezbollah", "nasrallah", "lebanon"]),
    ("Iran/Middle East", ["iran", "tehran", "khamenei", "ayatollah",
                          "houthi", "yemen", "gaza", "hamas", "hormuz",
                          "israel"]),
    ("Russia/Ukraine",   ["russia", "ukraine", "putin", "zelensky", "kyiv"]),
    ("Hungary",          ["hungary", "orban", "orbán"]),
    ("US politics",      ["trump", "biden", "harris", "kamala", "vance",
                          "desantis", "gop", "democrat", "republican",
                          "senate", "congress", "supreme court", "potus",
                          "fed chair", "fed chairman", "tariff",
                          "us x ", "us strikes", "us forces", "us obtains",
                          "u.s. "]),
    ("NBA",              ["nba", "lakers", "celtics", "warriors", "knicks",
                          "nuggets", "bucks", "76ers", "sixers", "thunder"]),
    ("College sports",   ["ncaa", "college football", "college basketball",
                          "march madness", "vanderbilt", "cornhuskers"]),
]


def bucket(title):
    t = (title or "").lower()
    for name, kws in TOPICS:
        if any(kw in t for kw in kws):
            return name
    return "Other"


def money(x):
    return f"${x:,.2f}"


def main():
    if len(sys.argv) != 2:
        print("usage: python analyze_wallet.py <wallet_address>")
        sys.exit(1)
    address = sys.argv[1]

    print(f"Fetching activity for {address}…")
    redeems     = fetch_activity(address, "REDEEM")
    merges      = fetch_activity(address, "MERGE")
    conversions = fetch_activity(address, "CONVERSION")
    positions   = fetch_positions(address)
    print(f"  REDEEMs:     {len(redeems):>4}   ${sum(e['usdcSize'] for e in redeems):>12,.2f}")
    print(f"  MERGEs:      {len(merges):>4}   ${sum(e['usdcSize'] for e in merges):>12,.2f}")
    print(f"  CONVERSIONs: {len(conversions):>4}   ${sum(e['usdcSize'] for e in conversions):>12,.2f}")
    print(f"  Open positions snapshot: {len(positions)}")

    # Discover every condition_id touched by the wallet
    cids = set()
    titles = {}
    for e in redeems + merges + conversions:
        if e.get("conditionId"):
            cids.add(e["conditionId"])
            titles[e["conditionId"]] = e.get("title") or titles.get(e["conditionId"], "")
    for p in positions:
        cids.add(p["conditionId"])
        titles[p["conditionId"]] = p.get("title") or titles.get(p["conditionId"], "")
    print(f"  Distinct markets to fetch trades for: {len(cids)}")

    # Inflow aggregation by conditionId
    inflow = defaultdict(lambda: {"redeem": 0.0, "merge": 0.0, "conversion": 0.0,
                                  "redeem_shares": 0.0})
    for e in redeems:
        inflow[e["conditionId"]]["redeem"]        += float(e["usdcSize"])
        inflow[e["conditionId"]]["redeem_shares"] += float(e.get("size") or 0)
    for e in merges:
        inflow[e["conditionId"]]["merge"]      += float(e["usdcSize"])
    for e in conversions:
        inflow[e["conditionId"]]["conversion"] += float(e["usdcSize"])

    # Index /positions by conditionId so we can fold in "redeemable but
    # never claimed" settlement values (mostly losing shares the wallet
    # didn't bother to redeem, but occasionally unclaimed wins too).
    pos_by_cid = defaultdict(list)
    for p in positions:
        pos_by_cid[p["conditionId"]].append(p)

    # Per-market trades — this is the slow step (~1 API call per market)
    print(f"\nFetching per-market trades ({len(cids)} markets)…")
    market = {}
    t0 = time.time()
    for i, cid in enumerate(sorted(cids), 1):
        trades = fetch_market_trades(address, cid)
        buys_usdc = sum(float(t["size"]) * float(t["price"]) for t in trades if t["side"] == "BUY")
        sells_usdc = sum(float(t["size"]) * float(t["price"]) for t in trades if t["side"] == "SELL")
        buy_shares  = sum(float(t["size"]) for t in trades if t["side"] == "BUY")
        sell_shares = sum(float(t["size"]) for t in trades if t["side"] == "SELL")
        # outcome the wallet was on: whichever asset has the most net buy shares
        asset_net = defaultdict(float)
        for t in trades:
            sign = 1 if t["side"] == "BUY" else -1
            asset_net[(t["asset"], t.get("outcome", "?"))] += sign * float(t["size"])
        outcome = max(asset_net.items(), key=lambda kv: kv[1])[0][1] if asset_net else None
        avg_entry = (buys_usdc / buy_shares) if buy_shares > 0 else 0.0
        title = titles.get(cid) or (trades[0].get("title") if trades else "") or ""
        if not titles.get(cid):
            titles[cid] = title

        # Settlement value of shares held in /positions where the market
        # resolved (redeemable=true) but the wallet hasn't redeemed. For
        # losers at curPrice≈0 this is $0 — the loss is realized through
        # the unrecovered cost basis. For unclaimed wins at curPrice≈1
        # we add the redemption value so realized reflects the true gain.
        unredeemed_settled = 0.0
        any_redeemable = False
        any_open       = False
        for p in pos_by_cid.get(cid, []):
            if p.get("redeemable"):
                any_redeemable = True
                unredeemed_settled += float(p.get("currentValue", 0))
            else:
                any_open = True

        market[cid] = {
            "title": title,
            "buys": buys_usdc, "sells": sells_usdc,
            "buy_shares": buy_shares, "sell_shares": sell_shares,
            "outcome": outcome, "avg_entry": avg_entry,
            "inflow_redeem": inflow[cid]["redeem"],
            "inflow_merge":  inflow[cid]["merge"],
            "inflow_conv":   inflow[cid]["conversion"],
            "redeem_shares": inflow[cid]["redeem_shares"],
            "unredeemed_settled": unredeemed_settled,
            "any_redeemable": any_redeemable,
            "any_open": any_open,
            "n_trades": len(trades),
        }
        if i % 25 == 0 or i == len(cids):
            print(f"  {i:>3}/{len(cids)} fetched  ({time.time()-t0:.1f}s)")

    # Compute realized P&L per market; classify open vs closed
    for cid, m in market.items():
        m["realized"] = (m["sells"] + m["inflow_redeem"] + m["inflow_merge"]
                         + m["inflow_conv"] + m["unredeemed_settled"] - m["buys"])
        m["net_shares"] = m["buy_shares"] - m["sell_shares"] - m["redeem_shares"]
        m["has_inflow"] = (m["inflow_redeem"] + m["inflow_merge"] + m["inflow_conv"]) > 0
        # closed = any settlement event, OR fully traded out, OR /positions
        # entry shows the market resolved (redeemable=true) without there
        # being any still-active position on it.
        m["closed"] = (m["has_inflow"]
                       or abs(m["net_shares"]) < 1
                       or (m["any_redeemable"] and not m["any_open"]))

    closed = [m for m in market.values() if m["closed"]]
    open_  = [m for m in market.values() if not m["closed"]]

    # ---------- summary ----------
    total_realized = sum(m["realized"] for m in closed)
    wins   = [m for m in closed if m["realized"] >  0]
    losses = [m for m in closed if m["realized"] <  0]
    flats  = [m for m in closed if m["realized"] == 0]
    won  = sum(m["realized"] for m in wins)
    lost = sum(m["realized"] for m in losses)
    win_rate = len(wins) / (len(wins) + len(losses)) * 100 if (wins or losses) else 0.0

    print()
    print("=" * 72)
    print(f"  WALLET: {address}")
    print("=" * 72)
    print(f"  Distinct markets touched:  {len(market)}")
    print(f"  Closed markets:            {len(closed)}")
    print(f"  Open markets:              {len(open_)}")
    print()
    print("CLOSED MARKETS — REALIZED P&L")
    print("-" * 72)
    print(f"  Wins:   {len(wins):>4}    total won  {money(won):>14}    "
          f"win rate {win_rate:.1f}%")
    print(f"  Losses: {len(losses):>4}    total lost {money(lost):>14}    "
          f"avg loss {money(lost/len(losses) if losses else 0)}")
    if flats:
        print(f"  Flats:  {len(flats):>4}    (realized == 0)")
    print(f"  NET REALIZED P&L: {money(total_realized)}")
    print()

    # ---------- top 10 wins ----------
    print("TOP 10 WINS — most likely informational edge")
    print("-" * 72)
    print(f"  {'realized':>12}  {'outcome':<5}  {'entry':>6}  {'buys':>10}  title")
    for m in sorted(wins, key=lambda x: -x["realized"])[:10]:
        print(f"  {money(m['realized']):>12}  "
              f"{(m['outcome'] or '?'):<5}  "
              f"{m['avg_entry']:>6.4f}  "
              f"{money(m['buys']):>10}  "
              f"{(m['title'] or '')[:65]}")
    print()

    # ---------- every loss ----------
    print(f"EVERY LOSS (largest first) — {len(losses)} markets")
    print("-" * 72)
    print(f"  {'realized':>12}  {'outcome':<5}  {'entry':>6}  {'buys':>10}  title")
    for m in sorted(losses, key=lambda x: x["realized"]):
        print(f"  {money(m['realized']):>12}  "
              f"{(m['outcome'] or '?'):<5}  "
              f"{m['avg_entry']:>6.4f}  "
              f"{money(m['buys']):>10}  "
              f"{(m['title'] or '')[:65]}")
    print()

    # ---------- open positions ----------
    # Truly-open positions only: exclude redeemable=true (settled, folded
    # into closed bucket above via unredeemed_settled). Use the live
    # snapshot's cashPnl since open positions are mark-to-market.
    active = [p for p in positions if not p.get("redeemable")]
    underwater = [p for p in active if float(p.get("cashPnl", 0)) < 0]
    profitable = [p for p in active if float(p.get("cashPnl", 0)) > 0]
    total_under  = sum(float(p["cashPnl"]) for p in underwater)
    total_profit = sum(float(p["cashPnl"]) for p in profitable)
    print("OPEN POSITIONS — UNREALIZED (from live /positions snapshot)")
    print("-" * 72)
    print(f"  Underwater: {len(underwater):>3}    total {money(total_under)}")
    print(f"  Profitable: {len(profitable):>3}    total {money(total_profit)}")
    print()
    print("TOP 10 UNDERWATER POSITIONS")
    print("-" * 72)
    print(f"  {'unrealized':>13}  {'outcome':<5}  {'entry':>6}  {'now':>6}  title")
    for p in sorted(underwater, key=lambda x: float(x["cashPnl"]))[:10]:
        print(f"  {money(float(p['cashPnl'])):>13}  "
              f"{(p.get('outcome') or '?'):<5}  "
              f"{float(p.get('avgPrice', 0)):>6.4f}  "
              f"{float(p.get('curPrice', 0)):>6.4f}  "
              f"{(p.get('title') or '')[:60]}")
    print()

    # ---------- topic clustering ----------
    print("TOPIC CLUSTERING — closed markets (where the alpha lives)")
    print("-" * 72)
    buckets = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    for m in closed:
        b = bucket(m["title"])
        if m["realized"] > 0:
            buckets[b]["wins"] += 1
        elif m["realized"] < 0:
            buckets[b]["losses"] += 1
        buckets[b]["pnl"] += m["realized"]
    print(f"  {'topic':<22} {'wins':>5} {'losses':>7} {'realized P&L':>16}")
    for name, d in sorted(buckets.items(), key=lambda kv: -kv[1]["pnl"]):
        print(f"  {name:<22} {d['wins']:>5} {d['losses']:>7} "
              f"{money(d['pnl']):>16}")
    print()

    # ---------- pattern queries ----------
    print("PATTERN QUERIES")
    print("-" * 72)
    contrarian = [m for m in wins if 0 < m["avg_entry"] < 0.20]
    consensus  = [m for m in wins if m["avg_entry"] > 0.80]
    print(f"  Wins with entry < 20¢ (contrarian moonshots): "
          f"{len(contrarian)} markets, total won {money(sum(m['realized'] for m in contrarian))}")
    print(f"  Wins with entry > 80¢ (consensus-side bets):  "
          f"{len(consensus)} markets, total won {money(sum(m['realized'] for m in consensus))}")


if __name__ == "__main__":
    main()
