-- Role the investigator connects as. SELECT on the recon tables, nothing else.
-- psql -d recon -v agent_password=change-me -f sql/readonly_role.sql

CREATE ROLE breakbot_agent LOGIN PASSWORD :'agent_password';
GRANT CONNECT ON DATABASE recon TO breakbot_agent;
GRANT USAGE ON SCHEMA public TO breakbot_agent;

GRANT SELECT ON accounts, securities, trades, custodian_settlements, corporate_actions, ca_processing,
    custodian_ca_postings, prices, fx_rates, ibor_positions, custodian_positions, ibor_cash_balances,
    custodian_cash_balances, ibor_cash_activity, custodian_cash_activity, breaks
TO breakbot_agent;
-- break_labels deliberately left out

ALTER ROLE breakbot_agent SET default_transaction_read_only = on;
ALTER ROLE breakbot_agent SET statement_timeout = '5s';
ALTER ROLE breakbot_agent SET idle_in_transaction_session_timeout = '30s';

-- separate role/schema for the audit trail, written by the app, never by the agent's connection
CREATE SCHEMA IF NOT EXISTS breakbot_audit;
