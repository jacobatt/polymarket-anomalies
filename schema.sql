-- Polymarket anomaly dashboard - database schema.
-- Run once in the Supabase SQL Editor.

CREATE TABLE IF NOT EXISTS trades (
    id                TEXT PRIMARY KEY,    -- synthetic: {transactionHash}_{proxyWallet}_{asset}
    transaction_hash  TEXT NOT NULL,
    proxy_wallet      TEXT NOT NULL,
    asset             TEXT NOT NULL,
    condition_id      TEXT NOT NULL,
    side              TEXT NOT NULL,       -- 'BUY' or 'SELL'
    size              NUMERIC NOT NULL,
    price             NUMERIC NOT NULL,
    timestamp         BIGINT NOT NULL,
    title             TEXT,
    slug              TEXT,
    outcome           TEXT,
    outcome_index     SMALLINT,
    name              TEXT,
    pseudonym         TEXT,
    notional          NUMERIC GENERATED ALWAYS AS (size * price) STORED
);

CREATE INDEX IF NOT EXISTS idx_trades_condition_ts ON trades(condition_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_ts           ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_wallet       ON trades(proxy_wallet);
