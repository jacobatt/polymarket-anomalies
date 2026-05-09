# Polymarket anomaly dashboard

A scored feed of unusual trades on Polymarket - outsized positions, counter-trend whales, suspicious wallets - with drill-down by market.

Stack: Python + Streamlit + Supabase (Postgres) + GitHub Actions. All free.

Data source: the official Polymarket Data API (`https://data-api.polymarket.com/trades`). The earlier Goldsky subgraph was deprecated in April 2026.

## Quick start

```bash
# 1. Set up the environment
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 2. Configure secrets
cp .env.example .env
# Edit .env with your Supabase URL. POLYMARKET_API_URL has a working default.

# 3. Create the database schema
# Open Supabase SQL Editor, paste contents of schema.sql, run.

# 4. Pull initial data and launch
python ingest.py
streamlit run app.py
```

## Files

| File | Purpose |
|---|---|
| `ingest.py` | Pulls new trades from the Polymarket Data API into Postgres. Run on a schedule. |
| `score.py` | Computes per-trade anomaly scores from the database. |
| `app.py` | Streamlit dashboard - KPIs, anomaly feed, market drill-down. |
| `schema.sql` | Database schema. Run once in Supabase. |
| `.github/workflows/ingest.yml` | GitHub Actions cron - runs `ingest.py` every 5 minutes. |
| `.env.example` | Template for secrets. Copy to `.env` and fill in. |

## Data model

Each row in `trades` is keyed by a synthetic id of the form `{transactionHash}_{proxyWallet}_{asset}`, since the Data API doesn't expose a unique row id. Every trade carries the market metadata inline (`title`, `slug`, `outcome`, `outcome_index`) and the wallet identity (`proxy_wallet`, `name`, `pseudonym`), so no separate markets table is needed.

`side` is stored as text (`'BUY'` / `'SELL'`) to match the API.

## Deploy

1. **GitHub** - push this repo (public, so Streamlit Cloud free tier accepts it).
2. **Streamlit Cloud** - [share.streamlit.io](https://share.streamlit.io) -> New app -> point at `app.py` -> add `DATABASE_URL` under Secrets -> Deploy.
3. **GitHub Actions** - add `DATABASE_URL` under Settings > Secrets and variables > Actions. The workflow in `.github/workflows/ingest.yml` will start running every 5 minutes.

See `polymarket_dashboard_walkthrough.md` (one folder up) for the full ground-up guide.

## Anomaly signals (v1)

Each trade gets a score combining:

- **Size outlier** - `(notional - market_mean) / market_stddev`, clipped at zero.
- **Counter-trend** - buy direction opposite to 1h price drift. Worth +3 points.

Trades scoring above 5 land in the feed. Add more signals (concentration, wallet age, market share) by extending `score_recent()` in `score.py`.

## Where to take it next

- Per-wallet PnL and history page (`pages/wallet.py`) keyed off `proxy_wallet`.
- Replace the hand-tuned scoring with `IsolationForest` once you have labeled examples.
- Wire a Telegram or Discord webhook for high-score trades.
- Filter ingest to specific markets via the API's `market` query param if the firehose is too noisy.
