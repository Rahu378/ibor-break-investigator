-- Postgres DDL for the recon warehouse. `python -m breakbot.seed --url postgresql+psycopg://...`
-- runs this, then bulk-loads the synthetic data.

DROP TABLE IF EXISTS break_labels, breaks, custodian_cash_activity, ibor_cash_activity, custodian_cash_balances,
    ibor_cash_balances, custodian_positions, ibor_positions, fx_rates, prices, custodian_ca_postings, ca_processing,
    corporate_actions, custodian_settlements, trades, securities, accounts CASCADE;

CREATE TABLE accounts (
    account_id      text PRIMARY KEY,
    account_name    text NOT NULL,
    custodian       text NOT NULL
);

CREATE TABLE securities (
    security_id     integer PRIMARY KEY,
    ticker          text NOT NULL,
    isin            char(12) NOT NULL UNIQUE,
    name            text NOT NULL,
    currency        char(3) NOT NULL,
    country         char(2) NOT NULL
);

-- IBOR trade ledger. trade_type = TRADE | CA_ADJ (share events IBOR booked itself)
CREATE TABLE trades (
    trade_id        text PRIMARY KEY,
    account_id      text NOT NULL,
    security_id     integer NOT NULL REFERENCES securities,
    trade_type      text NOT NULL,
    side            text NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity        bigint NOT NULL,
    price           numeric(18, 4),
    currency        char(3) NOT NULL,
    trade_date      date NOT NULL,
    settle_date     date NOT NULL,
    counterparty    text,
    ca_ref          text,
    booked_by       text,
    booked_at       timestamp
);
CREATE INDEX ON trades (account_id, security_id, trade_date);

-- what the custodian did with each instruction
CREATE TABLE custodian_settlements (
    trade_ref        text NOT NULL,
    account_id       text NOT NULL,
    security_id      integer NOT NULL,
    side             text NOT NULL,
    matched_quantity bigint,
    settled_quantity bigint,
    status           text NOT NULL,  -- SETTLED | PENDING | UNMATCHED | FAILED | PARTIAL
    fail_reason      text,
    settled_on       date
);
CREATE INDEX ON custodian_settlements (trade_ref);
CREATE INDEX ON custodian_settlements (account_id, security_id);

CREATE TABLE corporate_actions (
    ca_id           text PRIMARY KEY,
    security_id     integer NOT NULL REFERENCES securities,
    ca_type         text NOT NULL,  -- SPLIT | STOCK_DIV | CASH_DIV
    announce_date   date,
    ex_date         date NOT NULL,
    record_date     date,
    pay_date        date,
    ratio_new       numeric,
    ratio_old       numeric,
    rate            numeric(18, 6),
    currency        char(3),
    status          text
);

CREATE TABLE ca_processing (
    ca_id           text NOT NULL,
    account_id      text NOT NULL,
    ibor_status     text NOT NULL,
    processed_at    timestamp
);

CREATE TABLE custodian_ca_postings (
    ca_id           text NOT NULL,
    account_id      text NOT NULL,
    security_id     integer NOT NULL,
    qty_change      bigint,
    cash_amount     numeric(18, 2),
    posted_on       date
);

CREATE TABLE prices (
    security_id     integer NOT NULL,
    price_date      date NOT NULL,
    source          text NOT NULL,  -- IBOR golden copy | VENDOR
    close_price     numeric(18, 4) NOT NULL,
    loaded_at       timestamp
);
CREATE INDEX ON prices (security_id, price_date);

CREATE TABLE fx_rates (
    currency        char(3) NOT NULL,
    rate_date       date NOT NULL,
    source          text NOT NULL,  -- WMR_4PM | INTRADAY_10AM
    rate_to_usd     numeric(18, 8) NOT NULL
);

CREATE TABLE ibor_positions (
    as_of           date NOT NULL,
    account_id      text NOT NULL,
    security_id     integer NOT NULL,
    quantity        bigint NOT NULL,
    price           numeric(18, 4),
    price_date      date,
    currency        char(3),
    local_mv        numeric(20, 2),
    fx_rate         numeric(18, 8),
    fx_source       text,
    fx_rate_date    date,
    base_mv         numeric(20, 2)
);

CREATE TABLE custodian_positions (
    as_of           date NOT NULL,
    account_id      text NOT NULL,
    security_id     integer NOT NULL,
    quantity        bigint NOT NULL,
    price           numeric(18, 4),
    currency        char(3),
    local_mv        numeric(20, 2),
    fx_rate         numeric(18, 8),
    base_mv         numeric(20, 2)
);

CREATE TABLE ibor_cash_balances (
    as_of           date NOT NULL,
    account_id      text NOT NULL,
    fund_account_id text NOT NULL,
    currency        char(3) NOT NULL,
    balance         numeric(20, 2) NOT NULL
);
CREATE TABLE custodian_cash_balances (LIKE ibor_cash_balances);

CREATE TABLE ibor_cash_activity (
    entry_id        text PRIMARY KEY,
    account_id      text NOT NULL,
    currency        char(3) NOT NULL,
    value_date      date NOT NULL,
    entry_type      text NOT NULL,
    amount          numeric(20, 2) NOT NULL,
    reference       text
);
CREATE TABLE custodian_cash_activity (LIKE ibor_cash_activity INCLUDING ALL);

CREATE TABLE breaks (
    break_id        text PRIMARY KEY,
    as_of           date NOT NULL,
    status          text NOT NULL,
    account_id      text NOT NULL,
    security_id     integer,
    currency        char(3),
    break_type      text NOT NULL,  -- QTY | MV | CASH
    ibor_value      numeric(20, 2),
    custodian_value numeric(20, 2),
    difference      numeric(20, 2),
    difference_usd  numeric(20, 2)
);

-- the answer key for the benchmark. NOT granted to the agent role.
CREATE TABLE break_labels (
    break_id        text PRIMARY KEY REFERENCES breaks,
    root_cause      text NOT NULL,
    variant         text,
    ca_id           text
);
