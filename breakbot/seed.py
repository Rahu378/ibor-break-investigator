"""Synthetic IBOR / custodian recon warehouse.

How it works:
  1. Build one clean trade history and post it to both books (IBOR on trade
     date, custodian on settlement).
  2. Inject faults: corporate actions IBOR never processed, frozen prices,
     valuations run with the wrong FX fixing, trades still waiting to settle,
     failed settlements and booking errors.
  3. Run an ordinary recon (outer join + tolerance) over the two books.

The breaks come out of step 3. I never write them by hand. Each one is
labelled with the fault behind it, and the labels go into `break_labels`,
which the agent's SQL tools are not allowed to read.

Everything is deterministic for a given seed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import random
from collections import defaultdict
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

from .config import AS_OF, RECON_URL, ROOT

ROOT_CAUSES = ("TIMING", "MISSED_CA", "STALE_PRICE", "FX", "FAILED_SETTLEMENT", "BOOKING_ERROR")
MV_TOLERANCE_USD = 250.0
CASH_TOLERANCE = 0.01
TARGET_BREAKS = 200

# ticker, isin, name, currency, country, approx price (local)
SECURITIES = [
    ("AAPL", "US0378331005", "Apple Inc", "USD", "US", 228.40),
    ("MSFT", "US5949181045", "Microsoft Corp", "USD", "US", 415.10),
    ("NVDA", "US67066G1040", "NVIDIA Corp", "USD", "US", 142.60),
    ("AMZN", "US0231351067", "Amazon.com Inc", "USD", "US", 205.30),
    ("GOOGL", "US02079K3059", "Alphabet Inc Class A", "USD", "US", 172.80),
    ("META", "US30303M1027", "Meta Platforms Inc", "USD", "US", 575.20),
    ("JPM", "US46625H1005", "JPMorgan Chase & Co", "USD", "US", 238.70),
    ("V", "US92826C8394", "Visa Inc Class A", "USD", "US", 312.40),
    ("JNJ", "US4781601046", "Johnson & Johnson", "USD", "US", 156.20),
    ("PG", "US7427181091", "Procter & Gamble Co", "USD", "US", 171.50),
    ("KO", "US1912161007", "Coca-Cola Co", "USD", "US", 64.30),
    ("PEP", "US7134481081", "PepsiCo Inc", "USD", "US", 152.10),
    ("XOM", "US30231G1022", "Exxon Mobil Corp", "USD", "US", 118.90),
    ("CVX", "US1667641005", "Chevron Corp", "USD", "US", 158.40),
    ("BAC", "US0605051046", "Bank of America Corp", "USD", "US", 46.20),
    ("PFE", "US7170811035", "Pfizer Inc", "USD", "US", 26.10),
    ("MRK", "US58933Y1055", "Merck & Co Inc", "USD", "US", 99.30),
    ("CSCO", "US17275R1023", "Cisco Systems Inc", "USD", "US", 58.20),
    ("ORCL", "US68389X1054", "Oracle Corp", "USD", "US", 182.50),
    ("ADBE", "US00724F1012", "Adobe Inc", "USD", "US", 512.30),
    ("CRM", "US79466L3024", "Salesforce Inc", "USD", "US", 330.60),
    ("NFLX", "US64110L1061", "Netflix Inc", "USD", "US", 880.40),
    ("DIS", "US2546871060", "Walt Disney Co", "USD", "US", 112.70),
    ("WMT", "US9311421039", "Walmart Inc", "USD", "US", 88.10),
    ("TSM", "US8740391003", "Taiwan Semiconductor ADR", "USD", "US", 196.20),
    ("SAP", "DE0007164600", "SAP SE", "EUR", "DE", 228.10),
    ("SIE", "DE0007236101", "Siemens AG", "EUR", "DE", 182.30),
    ("ASML", "NL0010273215", "ASML Holding NV", "EUR", "NL", 668.40),
    ("MC", "FR0000121014", "LVMH Moet Hennessy", "EUR", "FR", 612.20),
    ("HSBA", "GB0005405286", "HSBC Holdings plc", "GBP", "GB", 7.42),
    ("AZN", "GB0009895292", "AstraZeneca plc", "GBP", "GB", 105.20),
    ("ULVR", "GB00B10RZP78", "Unilever plc", "GBP", "GB", 47.10),
    ("SHEL", "GB00BP6MXD84", "Shell plc", "GBP", "GB", 25.60),
    ("NESN", "CH0038863350", "Nestle SA", "CHF", "CH", 78.30),
    ("NOVN", "CH0012005267", "Novartis AG", "CHF", "CH", 92.50),
    ("7203", "JP3633400001", "Toyota Motor Corp", "JPY", "JP", 2780.0),
    ("6758", "JP3435000009", "Sony Group Corp", "JPY", "JP", 3270.0),
    ("RY", "CA7800871021", "Royal Bank of Canada", "CAD", "CA", 172.40),
    ("SHOP", "CA82509L1076", "Shopify Inc", "CAD", "CA", 158.00),
    ("BHP", "AU000000BHP4", "BHP Group Ltd", "AUD", "AU", 41.20),
    ("CBA", "AU000000CBA7", "Commonwealth Bank of Australia", "AUD", "AU", 158.30),
]

FX_SPOT = {"USD": 1.0, "EUR": 1.0842, "GBP": 1.2716, "CHF": 1.1318, "JPY": 0.006631, "CAD": 0.7194, "AUD": 0.6547}
LOT = {"JP": 100}

# account, name, custodian, style
ACCOUNTS = [
    ("GEQ-001", "Global Equity Fund", "Meridian Custody", "GLOBAL"),
    ("USL-002", "US Large Cap Core", "Meridian Custody", "US"),
    ("INT-003", "International Value", "Atlas Trust", "INTL"),
    ("DIV-004", "Global Dividend Income", "Atlas Trust", "GLOBAL"),
    ("TEC-005", "Technology Leaders", "Meridian Custody", "US"),
    ("EUR-006", "European Opportunities", "Atlas Trust", "EU"),
    ("BAL-007", "Balanced Allocation - Equity Sleeve", "Meridian Custody", "GLOBAL"),
    ("APE-008", "Asia Pacific Equity", "Atlas Trust", "APAC"),
    ("QNT-009", "Quant Multi-Factor", "Meridian Custody", "GLOBAL"),
    ("ESG-010", "Sustainable Leaders", "Atlas Trust", "GLOBAL"),
    ("SMC-011", "US Core Blend", "Meridian Custody", "US"),
    ("GRW-012", "Global Growth", "Atlas Trust", "GLOBAL"),
]

COUNTERPARTIES = [
    "Halden Securities", "Northgate Capital Markets", "Brightwater Brokerage",
    "Kestrel Prime", "Oakmont & Lowe", "Sable Street Partners", "Marlow Agency Execution",
]
TRADERS = ["jmalik", "kchen", "rosei", "dfarah", "lpetrov", "snair", "awong"]
FAIL_REASONS = [
    "Counterparty short of securities",
    "SSI mismatch at counterparty",
    "Counterparty insufficient cash",
    "Late matching instruction",
    "Place of settlement mismatch",
]


def isin_ok(isin: str) -> bool:
    """ISIN check digit (Luhn over the base-36 expansion)."""
    digits = "".join(str(int(c, 36)) for c in isin[:-1])
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10 == int(isin[-1])


def bday(d: dt.date, n: int) -> dt.date:
    """Move n business days (weekends only, no holiday calendar)."""
    step = 1 if n >= 0 else -1
    k = 0
    while k != n:
        d += dt.timedelta(days=step)
        if d.weekday() < 5:
            k += step
    return d


def settle_cycle(country: str) -> int:
    # Nov 2026: US/CA are T+1. UK/EU/CH don't move until Oct 2027. JP/AU are T+2.
    return 1 if country in ("US", "CA") else 2


class _Builder:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.trade_seq = 4_180_000
        self.ca_seq = 7_700
        self.entry_seq = 910_000
        self.trades: list[dict] = []
        self.settlements: list[dict] = []
        self.corporate_actions: list[dict] = []
        self.ca_processing: list[dict] = []
        self.cust_ca_postings: list[dict] = []
        self.ibor_cash_activity: list[dict] = []
        self.cust_cash_activity: list[dict] = []
        self.cash_opening: dict[tuple[str, str], float] = {}
        self.prices: list[dict] = []
        self.fx_rates: list[dict] = []
        self.ibor_fx_choice: dict[tuple[str, str], tuple[str, dt.date]] = {}
        # (kind, key) -> list of injected causes with details
        self.injected: dict[tuple, list[dict]] = defaultdict(list)
        self.touched: set[tuple[str, int]] = set()

    # ------------------------------------------------------------------ universe
    def universe(self) -> None:
        bad = [s[1] for s in SECURITIES if not isin_ok(s[1])]
        if bad:
            raise ValueError(f"bad ISIN check digits: {bad}")
        self.sec = pd.DataFrame(SECURITIES, columns=["ticker", "isin", "name", "currency", "country", "base_px"])
        self.sec.insert(0, "security_id", range(1001, 1001 + len(self.sec)))
        self.sec_by_id = {r.security_id: r for r in self.sec.itertuples()}
        self.acct = pd.DataFrame(ACCOUNTS, columns=["account_id", "account_name", "custodian", "style"])

        usd = list(self.sec[self.sec.currency == "USD"].security_id)
        eu = list(self.sec[self.sec.currency.isin(["EUR", "GBP", "CHF"])].security_id)
        apac = list(self.sec[self.sec.currency.isin(["JPY", "AUD"])].security_id)
        cad = list(self.sec[self.sec.currency == "CAD"].security_id)
        non_usd = eu + apac + cad
        rng = self.rng
        self.holdings: list[tuple[str, int]] = []
        for a in self.acct.itertuples():
            if a.style == "US":
                picks = rng.sample(usd, 21) + rng.sample(non_usd, 3)
            elif a.style == "INTL":
                picks = rng.sample(non_usd, 15) + rng.sample(usd, 6)
            elif a.style == "EU":
                picks = list(eu) + rng.sample(usd, 6)
            elif a.style == "APAC":
                picks = list(apac) + list(cad) + ["TSM"] + rng.sample(usd, 10)
                picks = [self.sec[self.sec.ticker == "TSM"].security_id.iloc[0] if p == "TSM" else p for p in picks]
            else:
                picks = rng.sample(usd, 16) + rng.sample(non_usd, 10)
            for s in sorted(set(picks)):
                self.holdings.append((a.account_id, int(s)))

    # ------------------------------------------------------------- market data
    def market_data(self) -> None:
        rng = self.rng
        self.dates = [bday(AS_OF, -k) for k in range(11, -1, -1)]  # 12 business days ending AS_OF
        self.vendor_px: dict[int, dict[dt.date, float]] = {}
        for s in self.sec.itertuples():
            px = s.base_px * rng.uniform(0.94, 1.04)
            series = {}
            for d in self.dates:
                px *= 1 + rng.gauss(0.0004, 0.013)
                series[d] = px
            self.vendor_px[s.security_id] = series
        self.wmr: dict[str, dict[dt.date, float]] = {}
        self.intraday: dict[str, dict[dt.date, float]] = {}
        for ccy, spot in FX_SPOT.items():
            if ccy == "USD":
                continue
            r = spot * rng.uniform(0.985, 1.015)
            ser, intr = {}, {}
            for d in self.dates:
                r *= 1 + rng.gauss(0, 0.0045)
                ser[d] = r
                intr[d] = r * (1 + rng.choice([-1, 1]) * rng.uniform(0.0016, 0.0042))
            # keep the T-1 vs T move visible so a stale fixing actually shows up
            prev = ser[self.dates[-2]]
            if abs(ser[AS_OF] / prev - 1) < 0.0018:
                ser[AS_OF] = prev * (1 + rng.choice([-1, 1]) * rng.uniform(0.0022, 0.0055))
            self.wmr[ccy] = ser
            self.intraday[ccy] = intr

    # ----------------------------------------------------------------- trading
    def _qty_for(self, sec_id: int, usd_target: float) -> int:
        s = self.sec_by_id[sec_id]
        lot = LOT.get(s.country, 1)
        raw = usd_target / (s.base_px * FX_SPOT[s.currency])
        step = 100 if raw > 2_000 else 10
        step = max(step, lot)
        return max(step, int(round(raw / step)) * step)

    def _new_trade(self, account, sec_id, side, qty, trade_date, *, price=None, trade_type="TRADE",
                   ca_ref=None, cust: dict | None = None, settle_date=None, booked_at=None) -> dict:
        s = self.sec_by_id[sec_id]
        self.trade_seq += self.rng.randint(1, 37)
        tid = f"T{self.trade_seq}"
        if settle_date is None:
            settle_date = trade_date if trade_type != "TRADE" else bday(trade_date, settle_cycle(s.country))
        if price is None:
            price = s.base_px * self.rng.uniform(0.93, 1.05)
        booked = booked_at or dt.datetime.combine(trade_date, dt.time(self.rng.randint(9, 16), self.rng.randint(0, 59)))
        t = dict(trade_id=tid, account_id=account, security_id=sec_id, trade_type=trade_type, side=side,
                 quantity=int(qty), price=round(price, 4), currency=s.currency, trade_date=trade_date,
                 settle_date=settle_date,
                 counterparty=None if trade_type != "TRADE" else self.rng.choice(COUNTERPARTIES),
                 ca_ref=ca_ref, booked_by=self.rng.choice(TRADERS) if trade_type == "TRADE" else "ca_batch",
                 booked_at=booked)
        self.trades.append(t)
        if trade_type == "TRADE":
            c = dict(trade_ref=tid, account_id=account, security_id=sec_id, side=side, matched_quantity=int(qty),
                     settled_quantity=int(qty), status="SETTLED", fail_reason=None, settled_on=settle_date)
            if cust:
                c.update(cust)
            self.settlements.append(c)
        return t

    def trade_history(self) -> None:
        rng = self.rng
        for account, sec_id in self.holdings:
            target = rng.uniform(0.35e6, 6.5e6)
            q0 = self._qty_for(sec_id, target)
            first = bday(AS_OF, -rng.randint(40, 70))
            self._new_trade(account, sec_id, "BUY", q0, first)
            pos = q0
            d = first
            for _ in range(rng.randint(1, 3)):
                d = bday(d, rng.randint(4, 12))
                if d > bday(AS_OF, -8):
                    break
                q = self._qty_for(sec_id, target * rng.uniform(0.08, 0.3))
                side = rng.choice(["BUY", "SELL"]) if pos > 2 * q else "BUY"
                self._new_trade(account, sec_id, side, q, d)
                pos += q if side == "BUY" else -q

    def ibor_qty_at(self, account, sec_id, d: dt.date) -> int:
        q = 0
        for t in self.trades:
            if t["account_id"] == account and t["security_id"] == sec_id and t["trade_date"] <= d:
                q += t["quantity"] if t["side"] == "BUY" else -t["quantity"]
        return q

    # ------------------------------------------------------- corporate actions
    def _new_ca(self, sec_id, ca_type, ex_date, pay_date, ratio_new=None, ratio_old=None, rate=None) -> dict:
        self.ca_seq += self.rng.randint(3, 19)
        s = self.sec_by_id[sec_id]
        ca = dict(ca_id=f"CA{self.ca_seq}", security_id=sec_id, ca_type=ca_type,
                  announce_date=bday(ex_date, -self.rng.randint(12, 25)), ex_date=ex_date,
                  record_date=bday(ex_date, 1) if ca_type == "CASH_DIV" else ex_date, pay_date=pay_date,
                  ratio_new=ratio_new, ratio_old=ratio_old, rate=rate, currency=s.currency, status="CONFIRMED")
        self.corporate_actions.append(ca)
        return ca

    def cash_openings(self) -> None:
        for account, sec_id in self.holdings:
            ccy = self.sec_by_id[sec_id].currency
            key = (account, ccy)
            if key not in self.cash_opening:
                usd = self.rng.uniform(15_000, 420_000)
                self.cash_opening[key] = round(usd / FX_SPOT[ccy], 2)

    def _cash_entry(self, book: list, account, ccy, value_date, entry_type, amount, reference):
        self.entry_seq += self.rng.randint(1, 9)
        book.append(dict(entry_id=f"E{self.entry_seq}", account_id=account, currency=ccy, value_date=value_date,
                         entry_type=entry_type, amount=round(amount, 2), reference=reference))

    def inject_corporate_actions(self) -> None:
        rng = self.rng
        holders = defaultdict(list)
        for account, sec_id in self.holdings:
            holders[sec_id].append(account)

        # --- share-quantity events: two splits and a stock dividend
        eligible = [s for s in holders if len(holders[s]) >= 5]
        rng.shuffle(eligible)
        qty_events = [("SPLIT", 4, 1), ("SPLIT", 2, 1), ("STOCK_DIV", 102, 100)]
        self.qty_ca_secs = []
        missed_budget = 16
        for (ca_type, rn, ro), sec_id in zip(qty_events, eligible):
            self.qty_ca_secs.append(sec_id)
            ex = bday(AS_OF, -rng.randint(2, 4))
            pay = ex if ca_type == "SPLIT" else bday(ex, 1)
            ca = self._new_ca(sec_id, ca_type, ex, pay, ratio_new=rn, ratio_old=ro)
            factor = rn / ro
            # split-adjust the vendor series from ex-date
            for d in self.dates:
                if d >= ex:
                    self.vendor_px[sec_id][d] /= factor
            accts = holders[sec_id][:]
            rng.shuffle(accts)
            n_missed = min(missed_budget, max(3, len(accts) * 2 // 3))
            missed_budget -= n_missed
            for i, account in enumerate(accts):
                qty = self.ibor_qty_at(account, sec_id, bday(ex, -1))
                entitled = int(qty * factor) - qty
                self.cust_ca_postings.append(dict(ca_id=ca["ca_id"], account_id=account, security_id=sec_id,
                                                  qty_change=entitled, cash_amount=None, posted_on=pay))
                self.touched.add((account, sec_id))
                if i < n_missed:
                    wrong = i == 0 and ca_type == "SPLIT"  # one account processed at the wrong ratio
                    if wrong:
                        bad_factor = (rn - 1) / ro if rn > 2 else 1.5
                        booked = int(qty * bad_factor) - qty
                        self._new_trade(account, sec_id, "BUY", booked, ex, price=0.0, trade_type="CA_ADJ",
                                        ca_ref=ca["ca_id"])
                        self.ca_processing.append(dict(ca_id=ca["ca_id"], account_id=account, ibor_status="PROCESSED",
                                                       processed_at=dt.datetime.combine(ex, dt.time(6, 40))))
                        variant = "wrong_ratio"
                    else:
                        self.ca_processing.append(dict(ca_id=ca["ca_id"], account_id=account,
                                                       ibor_status="NOT_PROCESSED", processed_at=None))
                        variant = "not_processed"
                    self.injected[("POS", account, sec_id)].append(
                        dict(cause="MISSED_CA", variant=variant, ca_id=ca["ca_id"], ca_type=ca_type))
                else:
                    self._new_trade(account, sec_id, "BUY", entitled, ex, price=0.0, trade_type="CA_ADJ",
                                    ca_ref=ca["ca_id"])
                    self.ca_processing.append(dict(ca_id=ca["ca_id"], account_id=account, ibor_status="PROCESSED",
                                                   processed_at=dt.datetime.combine(ex, dt.time(6, 40))))
            if missed_budget <= 0:
                break

        # --- cash dividends. Custodian always credits; IBOR misses some.
        div_secs = [s for s in holders if s not in self.qty_ca_secs]
        rng.shuffle(div_secs)
        div_secs = div_secs[:16]
        missed_combos: dict[tuple[str, str], int] = defaultdict(int)
        target_combos = 17
        pairs = []
        for sec_id in div_secs:
            s = self.sec_by_id[sec_id]
            ex = bday(AS_OF, -rng.randint(6, 9))
            pay = bday(ex, rng.randint(2, 5))
            if rng.random() < 0.2:
                pay = bday(AS_OF, rng.randint(2, 6))  # not paid yet: shows up in the CA table only
            px = self.vendor_px[sec_id][self.dates[0]]
            rate = round(px * rng.uniform(0.0035, 0.011), 4 if s.currency != "JPY" else 1)
            ca = self._new_ca(sec_id, "CASH_DIV", ex, pay, rate=rate)
            for account in holders[sec_id]:
                pairs.append((ca, account))
        rng.shuffle(pairs)
        double_left = 1  # one income account misses two dividends in the same currency
        gross_net_left = 2  # IBOR booked gross, custodian paid net of withholding
        for ca, account in pairs:
            sec_id = ca["security_id"]
            ccy = ca["currency"]
            if ca["pay_date"] > AS_OF:
                continue
            qty = self.ibor_qty_at(account, sec_id, ca["record_date"])
            gross = qty * ca["rate"]
            key = (account, ccy)
            variant = None
            if len(missed_combos) < target_combos and key not in missed_combos:
                if rng.random() < 0.45:
                    variant = "dividend_not_booked"
                elif gross_net_left and ccy != "USD" and rng.random() < 0.25:
                    variant = "booked_gross_paid_net"
                    gross_net_left -= 1
            elif key in missed_combos and double_left and rng.random() < 0.5:
                variant = "dividend_not_booked"
                double_left -= 1
            cust_amount = round(gross * 0.85, 2) if variant == "booked_gross_paid_net" else gross
            self._cash_entry(self.cust_cash_activity, account, ccy, ca["pay_date"], "DIVIDEND", cust_amount, ca["ca_id"])
            self.cust_ca_postings.append(dict(ca_id=ca["ca_id"], account_id=account, security_id=sec_id,
                                              qty_change=None, cash_amount=round(cust_amount, 2),
                                              posted_on=ca["pay_date"]))
            if variant:
                missed_combos[key] += 1
                self.injected[("CASH", account, ccy)].append(
                    dict(cause="MISSED_CA", variant=variant, ca_id=ca["ca_id"], ca_type="CASH_DIV"))
            if variant == "dividend_not_booked":
                self.ca_processing.append(dict(ca_id=ca["ca_id"], account_id=account, ibor_status="NOT_PROCESSED",
                                               processed_at=None))
            else:
                self._cash_entry(self.ibor_cash_activity, account, ccy, ca["pay_date"], "DIVIDEND", gross, ca["ca_id"])
                self.ca_processing.append(dict(ca_id=ca["ca_id"], account_id=account, ibor_status="PROCESSED",
                                               processed_at=dt.datetime.combine(ca["pay_date"], dt.time(7, 5))))

    # ---------------------------------------------------------------- pricing
    def inject_stale_prices(self, target_holdings: int = 33) -> None:
        rng = self.rng
        holders = defaultdict(list)
        for account, sec_id in self.holdings:
            holders[sec_id].append(account)
        cands = [s for s in holders if s not in self.qty_ca_secs]
        rng.shuffle(cands)
        self.stale: dict[int, dict] = {}
        total = 0
        for sec_id in cands:
            if total >= target_holdings:
                break
            n = len(holders[sec_id])
            if total + n > target_holdings + 3:
                continue
            variant = "feed_stopped" if len(self.stale) % 3 != 2 else "restamped_prior_close"
            stale_from = bday(AS_OF, -rng.randint(1, 3)) if variant == "feed_stopped" else AS_OF
            ser = self.vendor_px[sec_id]
            # make sure the move since the frozen point is big enough to matter
            ref = ser[stale_from] if variant == "feed_stopped" else ser[bday(AS_OF, -1)]
            if abs(ser[AS_OF] / ref - 1) < 0.006:
                ser[AS_OF] = ref * (1 + rng.choice([-1, 1]) * rng.uniform(0.008, 0.03))
            self.stale[sec_id] = dict(variant=variant, stale_from=stale_from)
            total += n
            for account in holders[sec_id]:
                self.injected[("POS", account, sec_id)].append(dict(cause="STALE_PRICE", variant=variant))

    def build_price_tables(self) -> None:
        for s in self.sec.itertuples():
            ser = self.vendor_px[s.security_id]
            st = self.stale.get(s.security_id)
            for d in self.dates:
                close = round(ser[d], 4)
                self.prices.append(dict(security_id=s.security_id, price_date=d, source="VENDOR", close_price=close,
                                        loaded_at=dt.datetime.combine(d, dt.time(21, 15))))
                ibor = close
                loaded = dt.datetime.combine(d, dt.time(21, 40))
                if st and st["variant"] == "feed_stopped" and d > st["stale_from"]:
                    continue  # golden copy never received the file
                if st and st["variant"] == "restamped_prior_close" and d == AS_OF:
                    ibor = round(ser[bday(AS_OF, -1)], 4)
                    loaded = dt.datetime.combine(d, dt.time(7, 5))
                self.prices.append(dict(security_id=s.security_id, price_date=d, source="IBOR", close_price=ibor,
                                        loaded_at=loaded))
        for ccy in self.wmr:
            for d in self.dates:
                self.fx_rates.append(dict(currency=ccy, rate_date=d, source="WMR_4PM", rate_to_usd=round(self.wmr[ccy][d], 8)))
                self.fx_rates.append(dict(currency=ccy, rate_date=d, source="INTRADAY_10AM",
                                          rate_to_usd=round(self.intraday[ccy][d], 8)))

    def inject_fx(self, target_holdings: int = 31) -> None:
        rng = self.rng
        combos = defaultdict(list)
        for account, sec_id in self.holdings:
            ccy = self.sec_by_id[sec_id].currency
            if ccy != "USD":
                # the wrong fixing hits every line in that currency, split names included
                combos[(account, ccy)].append(sec_id)
        keys = list(combos)
        rng.shuffle(keys)
        total = 0
        for key in keys:
            if total >= target_holdings:
                break
            n = len(combos[key])
            if total + n > target_holdings + 2:
                continue
            if rng.random() < 0.6:
                choice = ("WMR_4PM", bday(AS_OF, -1))
                variant = "prior_day_fixing"
            else:
                choice = ("INTRADAY_10AM", AS_OF)
                variant = "intraday_fixing"
            self.ibor_fx_choice[key] = choice
            total += n
            for sec_id in combos[key]:
                self.injected[("POS", key[0], sec_id)].append(dict(cause="FX", variant=variant))

    # ------------------------------------------------------ quantity faults
    def inject_qty_faults(self, n_breaks: int) -> None:
        rng = self.rng
        dirty = {(k[1], k[2]) for k in self.injected if k[0] == "POS"} | self.touched
        clean = [h for h in self.holdings if h not in dirty]
        rng.shuffle(clean)
        wrong_account = 3  # each one creates two breaks
        wrong_security = 1  # so does this
        n_timing = round(n_breaks * 38 / 104)
        n_failed = round(n_breaks * 36 / 104)
        n_booking = n_breaks - n_timing - n_failed - wrong_account - wrong_security  # injections, not breaks
        plan = ["TIMING"] * n_timing + ["FAILED_SETTLEMENT"] * n_failed + ["BOOKING_ERROR"] * n_booking
        rng.shuffle(plan)
        wa_slots = set(i for i, c in enumerate(plan) if c == "BOOKING_ERROR")
        picked = rng.sample(sorted(wa_slots), wrong_account + wrong_security)
        wa_slots, ws_slots = set(picked[:wrong_account]), set(picked[wrong_account:])
        held = set(self.holdings)
        used = set()
        it = iter(clean)
        for i, cause in enumerate(plan):
            account, sec_id = next(it)
            while (account, sec_id) in used:
                account, sec_id = next(it)
            used.add((account, sec_id))
            s = self.sec_by_id[sec_id]
            pos = self.ibor_qty_at(account, sec_id, AS_OF)
            q = self._qty_for(sec_id, rng.uniform(0.15e6, 2.5e6))
            side = rng.choice(["BUY", "SELL"]) if pos > 2 * q else "BUY"
            px = self.vendor_px[sec_id][AS_OF] * rng.uniform(0.995, 1.005)
            key = ("POS", account, sec_id)
            cyc = settle_cycle(s.country)

            if cause == "TIMING":
                td = AS_OF if (cyc == 1 or rng.random() < 0.5) else bday(AS_OF, -1)
                status = "PENDING" if rng.random() < 0.8 else "UNMATCHED"
                self._new_trade(account, sec_id, side, q, td, price=px, cust=dict(
                    matched_quantity=q if status == "PENDING" else None, settled_quantity=0, status=status,
                    settled_on=None))
                self.injected[key].append(dict(cause=cause, variant=f"t+{cyc}_{status.lower()}"))

            elif cause == "FAILED_SETTLEMENT":
                aged = rng.random() < 0.12
                td = bday(AS_OF, -rng.randint(14, 22)) if aged else bday(AS_OF, -rng.randint(cyc, cyc + 3))
                r = rng.random()
                if r < 0.55:
                    cust = dict(settled_quantity=0, status="FAILED", fail_reason=rng.choice(FAIL_REASONS), settled_on=None)
                    variant = "failed"
                elif r < 0.8:
                    cust = dict(settled_quantity=0, status="PENDING", fail_reason=None, settled_on=None)
                    variant = "past_due_pending"
                else:
                    part = int(q * rng.uniform(0.3, 0.8) / 10) * 10
                    cust = dict(settled_quantity=part, status="PARTIAL", fail_reason="Partial delivery - counterparty short")
                    variant = "partial"
                self._new_trade(account, sec_id, side, q, td, price=px, cust=cust)
                if "settled_on" not in cust:
                    self.settlements[-1]["settled_on"] = self.trades[-1]["settle_date"]
                self.injected[key].append(dict(cause=cause, variant=("aged_" if aged else "") + variant))

            else:  # BOOKING_ERROR
                td = bday(AS_OF, -rng.randint(cyc, cyc + 3))
                if i in wa_slots:
                    others = [a for a in self.acct.account_id if a != account and (a, sec_id) not in dirty
                              and (a, sec_id) not in used]
                    other = rng.choice(others)
                    used.add((other, sec_id))
                    self._new_trade(account, sec_id, "BUY", q, td, price=px, cust=dict(account_id=other))
                    self.injected[key].append(dict(cause=cause, variant="wrong_account_ibor_side", other=other))
                    self.injected[("POS", other, sec_id)].append(
                        dict(cause=cause, variant="wrong_account_custodian_side", other=account))
                    if (other, sec_id) not in held:
                        self.holdings.append((other, sec_id))
                        held.add((other, sec_id))
                    continue
                if i in ws_slots:
                    free = [x for x in self.sec.security_id if x != sec_id
                            and (account, x) not in dirty and (account, x) not in used]
                    sibs = [x for x in free if self.sec_by_id[x].currency == s.currency] or free
                    other_sec = int(rng.choice(sibs))
                    used.add((account, other_sec))
                    self._new_trade(account, sec_id, "BUY", q, td, price=px, cust=dict(security_id=other_sec))
                    self.injected[key].append(dict(cause=cause, variant="wrong_security_ibor_side"))
                    self.injected[("POS", account, other_sec)].append(
                        dict(cause=cause, variant="wrong_security_custodian_side"))
                    if (account, other_sec) not in held:
                        self.holdings.append((account, other_sec))
                        held.add((account, other_sec))
                    continue
                v = rng.choice(["fat_finger", "fat_finger", "transposed", "wrong_side"])
                # keep both books long: small positions only get buys, wrong-side trades get sized down
                side = "SELL" if pos > 12 * q and rng.random() < 0.35 else "BUY"
                if v == "fat_finger":
                    ibor_q = q // 10 if (q % 100 == 0 and rng.random() < 0.5) else q * 10
                    if side == "SELL" and ibor_q > q:
                        side = "BUY"
                    self._new_trade(account, sec_id, side, ibor_q, td, price=px)
                    self.settlements[-1].update(matched_quantity=q, settled_quantity=q)
                elif v == "transposed":
                    digits = list(str(q))
                    swaps = [k for k in range(len(digits) - 1)
                             if digits[k] != digits[k + 1] and not (k == 0 and digits[1] == "0")]
                    if not swaps:
                        q += 1300
                        digits = list(str(q))
                        swaps = [k for k in range(len(digits) - 1)
                                 if digits[k] != digits[k + 1] and not (k == 0 and digits[1] == "0")]
                    k = rng.choice(swaps)
                    digits[k], digits[k + 1] = digits[k + 1], digits[k]
                    ibor_q = int("".join(digits))
                    if side == "SELL" and ibor_q > q:
                        side = "BUY"
                    self._new_trade(account, sec_id, side, ibor_q, td, price=px)
                    self.settlements[-1].update(matched_quantity=q, settled_quantity=q)
                else:
                    lot = max(LOT.get(s.country, 1), 10)
                    q = min(q, max(lot, (pos // 3) // lot * lot))
                    ibor_side = rng.choice(["BUY", "SELL"])
                    cust_side = "SELL" if ibor_side == "BUY" else "BUY"
                    self._new_trade(account, sec_id, ibor_side, q, td, price=px)
                    self.settlements[-1].update(side=cust_side)
                self.injected[key].append(dict(cause=cause, variant=v))

    # ----------------------------------------------------------------- recon
    def positions(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        tr = pd.DataFrame(self.trades)
        tr = tr[tr.trade_date <= AS_OF].copy()
        tr["signed"] = tr.quantity.where(tr.side == "BUY", -tr.quantity)
        ibor_q = tr.groupby(["account_id", "security_id"]).signed.sum()

        st = pd.DataFrame(self.settlements)
        st = st[(st.settled_quantity > 0) & st.settled_on.notna()].copy()
        st = st[st.settled_on <= AS_OF]
        st["signed"] = st.settled_quantity.where(st.side == "BUY", -st.settled_quantity)
        cust_q = st.groupby(["account_id", "security_id"]).signed.sum()
        cap = pd.DataFrame(self.cust_ca_postings)
        cap = cap[cap.qty_change.notna() & (cap.posted_on <= AS_OF)]
        if len(cap):
            cust_q = cust_q.add(cap.groupby(["account_id", "security_id"]).qty_change.sum(), fill_value=0)

        ibor_px = {}
        pr = pd.DataFrame(self.prices)
        ib = pr[(pr.source == "IBOR") & (pr.price_date <= AS_OF)].sort_values("price_date")
        for sec_id, g in ib.groupby("security_id"):
            last = g.iloc[-1]
            ibor_px[sec_id] = (float(last.close_price), last.price_date)

        def fx(ccy, source="WMR_4PM", d=AS_OF):
            if ccy == "USD":
                return 1.0
            return round((self.wmr if source == "WMR_4PM" else self.intraday)[ccy][d], 8)

        keys = sorted(set(ibor_q.index) | set(cust_q.index))
        ib_rows, cu_rows = [], []
        for account, sec_id in keys:
            s = self.sec_by_id[sec_id]
            iq = int(ibor_q.get((account, sec_id), 0))
            cq = int(cust_q.get((account, sec_id), 0))
            vend = round(self.vendor_px[sec_id][AS_OF], 4)
            if iq:
                p, pdate = ibor_px[sec_id]
                src, fdate = self.ibor_fx_choice.get((account, s.currency), ("WMR_4PM", AS_OF))
                rate = fx(s.currency, src, fdate)
                ib_rows.append(dict(as_of=AS_OF, account_id=account, security_id=sec_id, quantity=iq, price=p,
                                    price_date=pdate, currency=s.currency, local_mv=round(iq * p, 2),
                                    fx_rate=rate, fx_source="USD" if s.currency == "USD" else src,
                                    fx_rate_date=AS_OF if s.currency == "USD" else fdate,
                                    base_mv=round(iq * p * rate, 2)))
            if cq:
                rate = fx(s.currency)
                cu_rows.append(dict(as_of=AS_OF, account_id=account, security_id=sec_id, quantity=cq, price=vend,
                                    currency=s.currency, local_mv=round(cq * vend, 2), fx_rate=rate,
                                    base_mv=round(cq * vend * rate, 2)))
        return pd.DataFrame(ib_rows), pd.DataFrame(cu_rows)

    def cash(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        def bal(book):
            df = pd.DataFrame(book)
            if df.empty:
                return {}
            df = df[df.value_date <= AS_OF]
            return df.groupby(["account_id", "currency"]).amount.sum().to_dict()
        ib, cu = bal(self.ibor_cash_activity), bal(self.cust_cash_activity)
        ib_rows, cu_rows = [], []
        for (account, ccy), opening in sorted(self.cash_opening.items()):
            ib_rows.append(dict(as_of=AS_OF, account_id=account + "-INC", fund_account_id=account, currency=ccy,
                                balance=round(opening + ib.get((account, ccy), 0.0), 2)))
            cu_rows.append(dict(as_of=AS_OF, account_id=account + "-INC", fund_account_id=account, currency=ccy,
                                balance=round(opening + cu.get((account, ccy), 0.0), 2)))
        return pd.DataFrame(ib_rows), pd.DataFrame(cu_rows)

    def recon(self):
        ibp, cup = self.positions()
        ibc, cuc = self.cash()
        m = ibp.merge(cup, on=["account_id", "security_id"], how="outer", suffixes=("_ibor", "_cust"))
        rows, labels = [], []
        for r in m.itertuples():
            iq = 0 if pd.isna(r.quantity_ibor) else int(r.quantity_ibor)
            cq = 0 if pd.isna(r.quantity_cust) else int(r.quantity_cust)
            s = self.sec_by_id[r.security_id]
            vend = self.vendor_px[r.security_id][AS_OF]
            wmr = 1.0 if s.currency == "USD" else self.wmr[s.currency][AS_OF]
            inj = self.injected.get(("POS", r.account_id, r.security_id), [])
            if iq != cq:
                diff = iq - cq
                rows.append(dict(account_id=r.account_id, security_id=r.security_id, currency=s.currency,
                                 break_type="QTY", ibor_value=iq, custodian_value=cq, difference=diff,
                                 difference_usd=round(diff * vend * wmr, 2)))
                qty_causes = [x for x in inj if x["cause"] not in ("STALE_PRICE", "FX")]
                if len(qty_causes) != 1:
                    raise RuntimeError(f"qty break without a single qty cause: {r.account_id} {s.ticker} {inj}")
                labels.append(qty_causes[0])
                continue
            ib_mv = 0.0 if pd.isna(r.base_mv_ibor) else r.base_mv_ibor
            cu_mv = 0.0 if pd.isna(r.base_mv_cust) else r.base_mv_cust
            diff = ib_mv - cu_mv
            if abs(diff) > MV_TOLERANCE_USD:
                rows.append(dict(account_id=r.account_id, security_id=r.security_id, currency="USD",
                                 break_type="MV", ibor_value=round(ib_mv, 2), custodian_value=round(cu_mv, 2),
                                 difference=round(diff, 2), difference_usd=round(diff, 2)))
                # price effect at IBOR fx + fx effect at vendor price == total
                price_eff = iq * (r.price_ibor - vend) * r.fx_rate_ibor
                fx_eff = iq * vend * (r.fx_rate_ibor - wmr)
                causes = {x["cause"]: x for x in inj}
                if not causes:
                    raise RuntimeError(f"MV break with no injected cause: {r.account_id} {s.ticker}")
                if "STALE_PRICE" in causes and "FX" in causes:
                    pick = "STALE_PRICE" if abs(price_eff) >= abs(fx_eff) else "FX"
                    lab = dict(causes[pick])
                    lab["variant"] = lab["variant"] + "+secondary_" + ("fx" if pick == "STALE_PRICE" else "stale_price")
                    labels.append(lab)
                else:
                    labels.append(next(iter(causes.values())))
        c = ibc.merge(cuc, on=["account_id", "fund_account_id", "currency"], suffixes=("_ibor", "_cust"))
        for r in c.itertuples():
            diff = round(r.balance_ibor - r.balance_cust, 2)
            if abs(diff) > CASH_TOLERANCE:
                rate = 1.0 if r.currency == "USD" else self.wmr[r.currency][AS_OF]
                rows.append(dict(account_id=r.account_id, security_id=None, currency=r.currency, break_type="CASH",
                                 ibor_value=r.balance_ibor, custodian_value=r.balance_cust, difference=diff,
                                 difference_usd=round(diff * rate, 2)))
                inj = self.injected.get(("CASH", r.fund_account_id, r.currency))
                if not inj:
                    raise RuntimeError(f"cash break with no injected cause: {r.account_id} {r.currency}")
                lab = dict(inj[0])
                if len(inj) > 1:
                    lab["variant"] = f"{len(inj)}_dividends_not_booked"
                labels.append(lab)
        return rows, labels, (ibp, cup, ibc, cuc)


def build_world(seed: int = 7) -> dict[str, pd.DataFrame]:
    b = _Builder(seed)
    b.universe()
    b.market_data()
    b.trade_history()
    b.cash_openings()
    b.inject_corporate_actions()
    b.inject_stale_prices()
    b.inject_fx()
    b.build_price_tables()
    rows, _, _ = b.recon()
    b.inject_qty_faults(TARGET_BREAKS - len(rows))
    rows, labels, (ibp, cup, ibc, cuc) = b.recon()
    if len(rows) != TARGET_BREAKS:
        raise RuntimeError(f"expected {TARGET_BREAKS} breaks, recon produced {len(rows)}")

    # stable but shuffled break ids so the queue isn't grouped by cause
    order = list(range(len(rows)))
    random.Random(seed + 1).shuffle(order)
    brk, lab = [], []
    for n, i in enumerate(order, start=1):
        bid = f"BRK-{n:04d}"
        brk.append(dict(break_id=bid, as_of=AS_OF, status="OPEN", **rows[i]))
        lab.append(dict(break_id=bid, root_cause=labels[i]["cause"], variant=labels[i]["variant"],
                        ca_id=labels[i].get("ca_id")))
    breaks = pd.DataFrame(brk).sort_values("break_id")
    breaks["security_id"] = breaks.security_id.astype("Int64")
    labels_df = pd.DataFrame(lab).sort_values("break_id")

    acct = b.acct.drop(columns=["style"])
    inc = acct.assign(account_id=acct.account_id + "-INC", account_name=acct.account_name + " (income cash)")
    tables = {
        "accounts": pd.concat([acct, inc], ignore_index=True),
        "securities": b.sec.drop(columns=["base_px"]),
        "trades": pd.DataFrame(b.trades),
        "custodian_settlements": pd.DataFrame(b.settlements).astype(
            {"matched_quantity": "Int64", "settled_quantity": "Int64"}),
        "corporate_actions": pd.DataFrame(b.corporate_actions),
        "ca_processing": pd.DataFrame(b.ca_processing),
        "custodian_ca_postings": pd.DataFrame(b.cust_ca_postings).astype({"qty_change": "Int64"}),
        "prices": pd.DataFrame(b.prices),
        "fx_rates": pd.DataFrame(b.fx_rates),
        "ibor_positions": ibp,
        "custodian_positions": cup,
        "ibor_cash_balances": ibc,
        "custodian_cash_balances": cuc,
        "ibor_cash_activity": pd.DataFrame(b.ibor_cash_activity).assign(
            account_id=lambda d: d.account_id + "-INC"),
        "custodian_cash_activity": pd.DataFrame(b.cust_cash_activity).assign(
            account_id=lambda d: d.account_id + "-INC"),
        "breaks": breaks,
        "break_labels": labels_df,
    }
    return tables


def write(tables: dict[str, pd.DataFrame], url: str = RECON_URL) -> None:
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    eng = create_engine(url)
    with eng.begin() as conn:
        if eng.dialect.name == "postgresql":
            conn.execute(text((ROOT / "sql" / "schema.sql").read_text()))
            for name, df in tables.items():
                df.to_sql(name, conn, if_exists="append", index=False)
        else:
            for name, df in tables.items():
                df.to_sql(name, conn, if_exists="replace", index=False)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="seed the synthetic recon warehouse")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--url", default=RECON_URL)
    args = ap.parse_args(argv)
    tables = build_world(args.seed)
    write(tables, args.url)
    lab = tables["break_labels"].root_cause.value_counts()
    print(f"seeded {len(tables['breaks'])} breaks into {args.url}")
    for cause in ROOT_CAUSES:
        print(f"  {cause:<18} {lab.get(cause, 0)}")


if __name__ == "__main__":
    main()
