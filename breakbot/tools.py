"""Read-only SQL tools the investigator is allowed to use.

Two layers of protection, because I don't want to rely on either alone:

  1. `check_sql` parses every statement with sqlglot and rejects anything that
     isn't a single SELECT over allow-listed tables. It also adds a LIMIT.
  2. The connection itself is read-only (sqlite `mode=ro`, or a Postgres
     session with `default_transaction_read_only` + a SELECT-only role, see
     sql/readonly_role.sql).

Every call is recorded as a QueryRecord with an id (Q1, Q2, ...). Findings
have to cite those ids and row numbers, so a reviewer can trace each claim back
to the rows behind it.
"""
from __future__ import annotations

import datetime as dt
import decimal
import numbers
import re
import time
from dataclasses import asdict, dataclass, field

import sqlglot
from sqlalchemy import create_engine, event, text
from sqlglot import exp

from .config import AS_OF, MAX_ROWS, RECON_URL, STATEMENT_TIMEOUT_MS

ALLOWED_TABLES = frozenset({
    "accounts", "securities", "trades", "custodian_settlements", "corporate_actions", "ca_processing",
    "custodian_ca_postings", "prices", "fx_rates", "ibor_positions", "custodian_positions",
    "ibor_cash_balances", "custodian_cash_balances", "ibor_cash_activity", "custodian_cash_activity", "breaks",
})
# break_labels is the answer key. The agent never sees it.

_WRITE_NODES = tuple(getattr(exp, n) for n in (
    "Insert", "Update", "Delete", "Drop", "Create", "Alter", "Merge", "Command", "TruncateTable",
    "Grant", "Copy", "Set", "Pragma", "Attach", "Detach", "Transaction", "Commit", "Rollback",
) if hasattr(exp, n))
_BANNED_FUNCS = {"load_extension", "pg_sleep", "pg_read_file", "pg_ls_dir", "lo_import", "lo_export",
                 "dblink", "readfile", "writefile", "randomblob", "set_config"}
_ID = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")


class GuardError(ValueError):
    pass


def check_sql(sql: str, dialect: str = "sqlite", max_rows: int = MAX_ROWS) -> str:
    """Return a safe, LIMITed version of `sql` or raise GuardError."""
    try:
        parsed = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except sqlglot.errors.ParseError as e:
        raise GuardError(f"could not parse SQL: {str(e).splitlines()[0]}") from None
    if len(parsed) != 1:
        raise GuardError(f"expected exactly one statement, got {len(parsed)}")
    tree = parsed[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise GuardError(f"only SELECT is allowed (got {type(tree).__name__.upper()})")
    for node in tree.walk():
        if isinstance(node, _WRITE_NODES):
            raise GuardError(f"{type(node).__name__.upper()} is not allowed")
        if isinstance(node, (exp.Anonymous, exp.Func)):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name in _BANNED_FUNCS:
                raise GuardError(f"function {name}() is not allowed")
        if isinstance(node, exp.Into):
            raise GuardError("SELECT ... INTO is not allowed")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        name = t.name.lower()
        if name in ctes:
            continue
        if t.args.get("db") and t.args["db"].name.lower() not in ("main", "public", "recon"):
            raise GuardError(f"schema '{t.args['db'].name}' is not allowed")
        if name not in ALLOWED_TABLES:
            raise GuardError(f"table '{name}' is not on the allow-list")
    if isinstance(tree, exp.Select):
        lim = tree.args.get("limit")
        if lim is None:
            tree = tree.limit(max_rows)
        else:
            try:
                n = int(lim.expression.name)
            except (AttributeError, ValueError):
                n = max_rows + 1
            if n > max_rows:
                tree = tree.limit(max_rows)
    else:
        tree = exp.select("*").from_(tree.subquery("u")).limit(max_rows)
    return tree.sql(dialect=dialect, pretty=True)


def lit(v) -> str:
    """Render a literal for the templated helper queries."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, numbers.Integral):
        return str(int(v))
    if isinstance(v, numbers.Real):
        return repr(float(v))
    if isinstance(v, (dt.date, dt.datetime)):
        return f"'{v.isoformat()}'"
    s = str(v)
    if not _ID.match(s):
        raise GuardError(f"refusing odd identifier {s!r}")
    return f"'{s}'"


def read_only_engine(url: str = RECON_URL):
    if url.startswith("sqlite:///"):
        path = url.removeprefix("sqlite:///")
        return create_engine(f"sqlite:///file:{path}?mode=ro&uri=true")
    eng = create_engine(url, pool_pre_ping=True)
    if eng.dialect.name == "postgresql":
        @event.listens_for(eng, "connect")
        def _ro(dbapi_conn, _):  # noqa: ARG001
            cur = dbapi_conn.cursor()
            cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            cur.execute(f"SET statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
            cur.close()
    return eng


def _jsonable(v):
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    return v


@dataclass
class QueryRecord:
    qid: str
    tool: str
    purpose: str
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    elapsed_ms: float = 0.0
    error: str | None = None

    def dicts(self) -> list[dict]:
        return [dict(zip(self.columns, r)) for r in self.rows]

    def as_dict(self, max_rows: int | None = None) -> dict:
        d = asdict(self)
        if max_rows is not None:
            d["truncated"] = len(self.rows) > max_rows
            d["rows"] = d["rows"][:max_rows]
        d["row_count"] = len(self.rows)
        return d

    def for_model(self, max_rows: int = 50) -> str:
        """Compact text for an LLM tool result."""
        if self.error:
            return f"{self.qid} ERROR: {self.error}"
        head = f"{self.qid} ({len(self.rows)} rows) columns: {', '.join(self.columns)}"
        body = "\n".join(f"r{i}: " + " | ".join("" if v is None else str(v) for v in row)
                         for i, row in enumerate(self.rows[:max_rows]))
        more = f"\n... {len(self.rows) - max_rows} more rows" if len(self.rows) > max_rows else ""
        return f"{head}\n{body}{more}"


class ReconTools:
    """SQL access for one investigation. Holds the query log for citations."""

    def __init__(self, engine=None, as_of: dt.date = AS_OF, lookback_bd: int = 10, on_query=None):
        self.engine = engine or read_only_engine()
        self.dialect = "postgres" if self.engine.dialect.name == "postgresql" else "sqlite"
        self.as_of = as_of
        self.since = as_of - dt.timedelta(days=int(lookback_bd * 7 / 5) + 1)
        self.queries: list[QueryRecord] = []
        self.on_query = on_query

    # -- core ---------------------------------------------------------------
    def run_sql(self, sql: str, purpose: str, tool: str = "run_sql") -> QueryRecord:
        qid = f"Q{len(self.queries) + 1}"
        rec = QueryRecord(qid=qid, tool=tool, purpose=purpose, sql=sql.strip())
        t0 = time.perf_counter()
        try:
            safe = check_sql(sql, self.dialect)
            rec.sql = safe
            with self.engine.connect() as conn:
                res = conn.execute(text(safe))
                rec.columns = list(res.keys())
                rec.rows = [[_jsonable(v) for v in row] for row in res.fetchall()]
        except GuardError as e:
            rec.error = f"blocked by guard: {e}"
        except Exception as e:  # surfaced to the agent, not swallowed
            rec.error = f"{type(e).__name__}: {str(e).splitlines()[0][:300]}"
        rec.elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        self.queries.append(rec)
        if self.on_query:
            self.on_query(rec)
        return rec

    def get(self, qid: str) -> QueryRecord | None:
        for q in self.queries:
            if q.qid == qid:
                return q
        return None

    # -- templated helpers (still go through run_sql + the guard) ------------
    def break_detail(self, break_id: str) -> QueryRecord:
        return self.run_sql(f"""
            SELECT b.break_id, b.as_of, b.account_id, b.security_id, s.ticker, s.isin, s.currency AS sec_ccy,
                   s.country, b.currency, b.break_type, b.ibor_value, b.custodian_value, b.difference,
                   b.difference_usd, b.status
            FROM breaks b LEFT JOIN securities s ON s.security_id = b.security_id
            WHERE b.break_id = {lit(break_id)}""", "load the break", "break_detail")

    def positions(self, account_id: str, security_id: int) -> QueryRecord:
        return self.run_sql(f"""
            SELECT 'IBOR' AS book, quantity, price, price_date, fx_rate, fx_source, fx_rate_date, local_mv, base_mv
            FROM ibor_positions WHERE account_id = {lit(account_id)} AND security_id = {lit(security_id)}
            UNION ALL
            SELECT 'CUSTODIAN' AS book, quantity, price, as_of AS price_date, fx_rate, 'WMR_4PM' AS fx_source,
                   as_of AS fx_rate_date, local_mv, base_mv
            FROM custodian_positions WHERE account_id = {lit(account_id)} AND security_id = {lit(security_id)}""",
            "side-by-side position snapshot", "positions")

    def recent_trades(self, account_id: str, security_id: int) -> QueryRecord:
        return self.run_sql(f"""
            SELECT t.trade_id, t.trade_type, t.side, t.quantity, t.trade_date, t.settle_date, t.counterparty,
                   t.ca_ref, t.booked_by, s.account_id AS cust_account, s.side AS cust_side, s.matched_quantity,
                   s.settled_quantity, s.status AS cust_status, s.fail_reason, s.settled_on
            FROM trades t
            LEFT JOIN custodian_settlements s ON s.trade_ref = t.trade_id
            WHERE t.account_id = {lit(account_id)} AND t.security_id = {lit(security_id)}
              AND t.trade_date >= {lit(self.since)}
            ORDER BY t.trade_date, t.trade_id""", "IBOR trades in the lookback window + custodian status",
            "recent_trades")

    def foreign_settlements(self, account_id: str, security_id: int) -> QueryRecord:
        return self.run_sql(f"""
            SELECT s.trade_ref, s.account_id AS cust_account, s.side, s.matched_quantity, s.settled_quantity,
                   s.status, s.settled_on, t.account_id AS ibor_account, t.booked_by
            FROM custodian_settlements s
            LEFT JOIN trades t ON t.trade_id = s.trade_ref
            WHERE s.account_id = {lit(account_id)} AND s.security_id = {lit(security_id)}
              AND (t.account_id IS NULL OR t.account_id <> s.account_id)""",
            "custodian settlements in this account that IBOR booked somewhere else", "foreign_settlements")

    def corporate_actions(self, account_id: str, security_id: int) -> QueryRecord:
        return self.run_sql(f"""
            SELECT ca.ca_id, ca.ca_type, ca.ex_date, ca.pay_date, ca.ratio_new, ca.ratio_old, ca.rate,
                   p.ibor_status, p.processed_at, cp.qty_change AS cust_qty_change, cp.cash_amount AS cust_cash
            FROM corporate_actions ca
            LEFT JOIN ca_processing p ON p.ca_id = ca.ca_id AND p.account_id = {lit(account_id)}
            LEFT JOIN custodian_ca_postings cp ON cp.ca_id = ca.ca_id AND cp.account_id = {lit(account_id)}
            WHERE ca.security_id = {lit(security_id)} AND ca.ex_date >= {lit(self.since)}
            ORDER BY ca.ex_date""", "corporate actions on the security, with IBOR/custodian processing",
            "corporate_actions")

    def prices(self, security_id: int) -> QueryRecord:
        return self.run_sql(f"""
            SELECT price_date, source, close_price, loaded_at FROM prices
            WHERE security_id = {lit(security_id)} AND price_date >= {lit(self.as_of - dt.timedelta(days=6))}
            ORDER BY price_date DESC, source""", "IBOR golden copy vs vendor closes", "prices")

    def fx(self, currency: str) -> QueryRecord:
        return self.run_sql(f"""
            SELECT rate_date, source, rate_to_usd FROM fx_rates
            WHERE currency = {lit(currency)} AND rate_date >= {lit(self.as_of - dt.timedelta(days=4))}
            ORDER BY rate_date DESC, source""", f"{currency}/USD fixings around the as-of date", "fx")

    def unmatched_cash(self, account_id: str, currency: str) -> QueryRecord:
        return self.run_sql(f"""
            SELECT c.entry_id, c.value_date, c.entry_type, c.amount, c.reference, 'CUSTODIAN_ONLY' AS side
            FROM custodian_cash_activity c
            LEFT JOIN ibor_cash_activity i ON i.account_id = c.account_id AND i.reference = c.reference
            WHERE c.account_id = {lit(account_id)} AND c.currency = {lit(currency)}
              AND c.value_date >= {lit(self.since)} AND i.entry_id IS NULL
            UNION ALL
            SELECT i.entry_id, i.value_date, i.entry_type, i.amount, i.reference, 'IBOR_ONLY' AS side
            FROM ibor_cash_activity i
            LEFT JOIN custodian_cash_activity c ON c.account_id = i.account_id AND c.reference = i.reference
            WHERE i.account_id = {lit(account_id)} AND i.currency = {lit(currency)}
              AND i.value_date >= {lit(self.since)} AND c.entry_id IS NULL""",
            "cash entries on one side only", "unmatched_cash")

    def cash_balances(self, account_id: str, currency: str) -> QueryRecord:
        return self.run_sql(f"""
            SELECT 'IBOR' AS book, balance FROM ibor_cash_balances
            WHERE account_id = {lit(account_id)} AND currency = {lit(currency)}
            UNION ALL
            SELECT 'CUSTODIAN' AS book, balance FROM custodian_cash_balances
            WHERE account_id = {lit(account_id)} AND currency = {lit(currency)}""",
            "income cash balance on both books", "cash_balances")

    def ca_detail(self, ca_ids: list[str], fund_account: str) -> QueryRecord:
        ids = ", ".join(lit(c) for c in ca_ids) or "''"
        return self.run_sql(f"""
            SELECT ca.ca_id, ca.ca_type, s.ticker, ca.ex_date, ca.record_date, ca.pay_date, ca.rate, ca.currency,
                   p.ibor_status, cp.cash_amount AS cust_cash
            FROM corporate_actions ca
            JOIN securities s ON s.security_id = ca.security_id
            LEFT JOIN ca_processing p ON p.ca_id = ca.ca_id AND p.account_id = {lit(fund_account)}
            LEFT JOIN custodian_ca_postings cp ON cp.ca_id = ca.ca_id AND cp.account_id = {lit(fund_account)}
            WHERE ca.ca_id IN ({ids})""", "what the unmatched references are", "ca_detail")

    def schema(self) -> str:
        """Table/column listing for the LLM prompt."""
        lines = []
        with self.engine.connect() as conn:
            for t in sorted(ALLOWED_TABLES):
                cols = conn.execute(text(f"SELECT * FROM {t} LIMIT 0")).keys()
                lines.append(f"{t}({', '.join(cols)})")
        return "\n".join(lines)
