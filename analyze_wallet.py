"""One-off analysis: structured breakdown of a wallet's full positions book.

Fetches https://data-api.polymarket.com/positions?user=...&limit=500 with
offset pagination, then prints W/L, unrealized exposure, topic clustering,
and contrarian / consensus pattern queries.

Positions API field names (probed live, not guessed):
  realizedPnl     — closed-portion $ P&L
  cashPnl         — unrealized $ P&L on held shares (currentValue-initialValue)
  avgPrice        — entry price (0–1)
  curPrice        — current price (0–1)
  size            — shares still held (0 means sold out / closed)
  totalBought     — cumulative $ spent on this market across all buys
  redeemable      — market has resolved and shares can be cashed out
  outcome         — "Yes" / "No" the wallet is holding
  title           — human-readable market title

Polymarket positions are net-long aggregates: there's no BUY/SELL field on a
position, so directional info here is the `outcome` they hold (Yes/No).

Run with: python analyze_wallet.py 0xWALLETADDR
"""
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()  # not strictly needed here, but keeps the venv pattern consistent

DATA_URL = "https://data-api.polymarket.com/positions"
PAGE = 500
MAX_OFFSET = 100_000


def fetch_positions(address: str):
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
                print(f"  Pagination cap hit at offset {offset}, "
                      f"returning {len(out)} positions fetched so far")
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


def is_closed(p) -> bool:
    """A position is 'closed' if shares are gone (sold out) or the market
    resolved (redeemable). Both states mean realizedPnl is the final word."""
    return float(p.get("size", 0)) == 0 or bool(p.get("redeemable", False))


# Topic buckets. First match wins (Israel/Hezbollah before Iran/Middle East
# so e.g. "Hezbollah leader" doesn't get swallowed by a broad Iran keyword).
TOPICS = [
    ("Israel/Hezbollah", ["hezbollah", "nasrallah", "lebanon"]),
    ("Iran/Middle East", ["iran", "tehran", "khamenei", "ayatollah",
                          "houthi", "yemen", "gaza", "hamas", "israel"]),
    ("Russia/Ukraine",   ["russia", "ukraine", "putin", "zelensky", "kyiv",
                          "moscow"]),
    ("Hungary",          ["hungary", "orban", "orbán"]),
    ("US politics",      ["trump", "biden", "harris", "kamala", "vance",
                          "desantis", "gop", "democrat", "republican",
                          "senate", "congress", "supreme court", "potus",
                          "white house", "u.s. ", "us strikes", "us x ",
                          "tariff"]),
    ("NBA",              ["nba", "lakers", "celtics", "warriors", "knicks",
                          "nuggets", "heat ", "bucks", "76ers", "sixers"]),
    ("College sports",   ["ncaa", "college football", "college basketball",
                          "march madness"]),
]


def bucket(title: str) -> str:
    t = (title or "").lower()
    for name, kws in TOPICS:
        if any(kw in t for kw in kws):
            return name
    return "Other"


def money(x) -> str:
    return f"${x:,.2f}"


def pct(x) -> str:
    return f"{x:.1f}%"


def main():
    if len(sys.argv) != 2:
        print("usage: python analyze_wallet.py <wallet_address>")
        sys.exit(1)
    address = sys.argv[1]

    positions = fetch_positions(address)
    print(f"Fetched {len(positions)} positions for {address}\n")

    closed = [p for p in positions if is_closed(p)]
    open_  = [p for p in positions if not is_closed(p)]

    print("=" * 72)
    print(f"  WALLET: {address}")
    print("=" * 72)
    print(f"  Total positions:        {len(positions)}")
    print(f"  Closed / redeemable:    {len(closed)}")
    print(f"  Open / active:          {len(open_)}")
    print()

    # ---------- closed W/L ----------
    wins   = [p for p in closed if float(p.get("realizedPnl", 0)) > 0]
    losses = [p for p in closed if float(p.get("realizedPnl", 0)) < 0]
    flats  = [p for p in closed if float(p.get("realizedPnl", 0)) == 0]
    won  = sum(float(p["realizedPnl"]) for p in wins)
    lost = sum(float(p["realizedPnl"]) for p in losses)  # negative
    total_realized = sum(float(p.get("realizedPnl", 0)) for p in closed)
    win_rate = len(wins) / (len(wins) + len(losses)) * 100 if (wins or losses) else 0.0
    avg_loss = lost / len(losses) if losses else 0.0

    print("CLOSED POSITIONS — W/L")
    print("-" * 72)
    print(f"  Wins:   {len(wins):>4}   total won  {money(won):>14}   "
          f"win rate {pct(win_rate)}")
    print(f"  Losses: {len(losses):>4}   total lost {money(lost):>14}   "
          f"avg loss {money(avg_loss)}")
    if flats:
        print(f"  Flats:  {len(flats):>4}   (realizedPnl == 0)")
    print(f"  Net realized P&L: {money(total_realized)}")
    print()

    # ---------- every loss, largest first ----------
    print("EVERY LOSS (largest first) — the smoking-gun list")
    print("-" * 72)
    print(f"  {'realizedPnl':>13}  {'outcome':<7}  {'entry':>6}  "
          f"{'totalBought':>13}  title")
    for p in sorted(losses, key=lambda x: float(x["realizedPnl"])):
        print(f"  {money(float(p['realizedPnl'])):>13}  "
              f"{(p.get('outcome') or '?').upper():<7}  "
              f"{float(p.get('avgPrice', 0)):>6.4f}  "
              f"{money(float(p.get('totalBought', 0))):>13}  "
              f"{(p.get('title') or '')[:80]}")
    print()

    # ---------- open unrealized ----------
    underwater   = [p for p in open_ if float(p.get("cashPnl", 0)) < 0]
    profitable   = [p for p in open_ if float(p.get("cashPnl", 0)) > 0]
    total_under  = sum(float(p["cashPnl"]) for p in underwater)
    total_profit = sum(float(p["cashPnl"]) for p in profitable)

    print("OPEN POSITIONS — UNREALIZED P&L")
    print("-" * 72)
    print(f"  Underwater:  {len(underwater):>4}   "
          f"total unrealized loss {money(total_under)}")
    print(f"  Profitable:  {len(profitable):>4}   "
          f"total unrealized gain {money(total_profit)}")
    print()

    print("TOP 10 UNDERWATER OPEN POSITIONS — the bag-holders")
    print("-" * 72)
    print(f"  {'unrealized':>13}  {'outcome':<7}  {'entry':>6}  "
          f"{'now':>6}  title")
    for p in sorted(underwater, key=lambda x: float(x["cashPnl"]))[:10]:
        print(f"  {money(float(p['cashPnl'])):>13}  "
              f"{(p.get('outcome') or '?').upper():<7}  "
              f"{float(p.get('avgPrice', 0)):>6.4f}  "
              f"{float(p.get('curPrice', 0)):>6.4f}  "
              f"{(p.get('title') or '')[:80]}")
    print()

    # ---------- topic clustering of closed ----------
    print("TOPIC CLUSTERING — closed positions (where the alpha lives)")
    print("-" * 72)
    buckets = {}
    for p in closed:
        b = bucket(p.get("title", ""))
        d = buckets.setdefault(b, {"wins": 0, "losses": 0, "pnl": 0.0})
        rpnl = float(p.get("realizedPnl", 0))
        if rpnl > 0:
            d["wins"] += 1
        elif rpnl < 0:
            d["losses"] += 1
        d["pnl"] += rpnl
    print(f"  {'topic':<22} {'wins':>5} {'losses':>7} {'realized P&L':>16}")
    for name, d in sorted(buckets.items(), key=lambda kv: -kv[1]["pnl"]):
        print(f"  {name:<22} {d['wins']:>5} {d['losses']:>7} "
              f"{money(d['pnl']):>16}")
    print()

    # ---------- pattern queries ----------
    print("PATTERN QUERIES")
    print("-" * 72)
    contrarian = [p for p in wins if float(p.get("avgPrice", 0)) < 0.20]
    consensus  = [p for p in wins if float(p.get("avgPrice", 0)) > 0.80]
    print(f"  Wins with entry < 20¢ (contrarian moonshots): "
          f"{len(contrarian)} positions, total won {money(sum(float(p['realizedPnl']) for p in contrarian))}")
    print(f"  Wins with entry > 80¢ (consensus-side bets):  "
          f"{len(consensus)} positions, total won {money(sum(float(p['realizedPnl']) for p in consensus))}")
    print()

    print("TOP 10 SINGLE-POSITION WINS — most likely informational edge")
    print("-" * 72)
    print(f"  {'realizedPnl':>13}  {'outcome':<7}  {'entry':>6}  "
          f"{'totalBought':>13}  title")
    for p in sorted(wins, key=lambda x: -float(x["realizedPnl"]))[:10]:
        print(f"  {money(float(p['realizedPnl'])):>13}  "
              f"{(p.get('outcome') or '?').upper():<7}  "
              f"{float(p.get('avgPrice', 0)):>6.4f}  "
              f"{money(float(p.get('totalBought', 0))):>13}  "
              f"{(p.get('title') or '')[:80]}")


if __name__ == "__main__":
    main()
