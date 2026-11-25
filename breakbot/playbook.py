"""Deterministic investigator.

This is the "senior recon analyst in a box" path: the same read-only tools the
LLM gets, walked in a fixed order, with every hypothesis scored by how much of
the break it explains. I use it for three things:

  * a baseline the LLM has to beat on the benchmark,
  * running the whole pipeline (graph, review, audit, UI) without an API key,
  * a cross-check: when the LLM and the playbook disagree, the break goes to a
    human with both explanations attached.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field

from .tools import QueryRecord, ReconTools

CAUSES = ("TIMING", "MISSED_CA", "STALE_PRICE", "FX", "FAILED_SETTLEMENT", "BOOKING_ERROR")

LABEL = {
    "TIMING": "Timing",
    "MISSED_CA": "Missed corporate action",
    "STALE_PRICE": "Stale price",
    "FX": "FX",
    "FAILED_SETTLEMENT": "Failed settlement",
    "BOOKING_ERROR": "Booking error",
    "UNEXPLAINED": "Unexplained",
}

OWNER = {
    "TIMING": "Reconciliations (monitor only)",
    "MISSED_CA": "Corporate Actions",
    "STALE_PRICE": "Pricing & Valuations",
    "FX": "Market Data - FX",
    "FAILED_SETTLEMENT": "Settlements",
    "BOOKING_ERROR": "Trade Support",
    "UNEXPLAINED": "Reconciliations",
}


@dataclass
class Evidence:
    qid: str
    row: int
    note: str


@dataclass
class Finding:
    root_cause: str
    confidence: float
    summary: str
    evidence: list[Evidence]
    proposed_fix: str
    owner_team: str
    hypotheses: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Investigation:
    break_id: str
    steps: list[dict]
    finding: Finding
    mode: str = "playbook"


def _d(v) -> dt.date | None:
    if v is None:
        return None
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v)[:10])


def _signed(side, qty) -> int:
    if qty is None:
        return 0
    return int(qty) if side == "BUY" else -int(qty)


def fmt_qty(q) -> str:
    return f"{int(q):,}"


def fmt_money(v, ccy="USD") -> str:
    return f"{ccy} {v:,.2f}"


def fmt_ratio(n, o) -> str:
    n, o = float(n), float(o)
    return f"{n:g}:{o:g}"


class _Run:
    def __init__(self, tools: ReconTools, break_id: str):
        self.t = tools
        self.break_id = break_id
        self.steps: list[dict] = []

    def think(self, text: str):
        self.steps.append(dict(kind="thought", text=text))

    def call(self, rec: QueryRecord, note: str | None = None) -> QueryRecord:
        self.steps.append(dict(kind="tool", qid=rec.qid, tool=rec.tool, text=rec.purpose))
        if note:
            self.observe(note)
        return rec

    def observe(self, text: str):
        self.steps.append(dict(kind="observation", text=text))


def _fit(diff: float, explained: float) -> float:
    if diff == 0:
        return 0.0
    return max(0.0, 1.0 - abs(diff - explained) / abs(diff))


# --------------------------------------------------------------------------- QTY
def _qty(run: _Run, b: dict, bq: QueryRecord) -> Finding:
    t = run.t
    acct, sec, tick = b["account_id"], b["security_id"], b["ticker"]
    diff = int(b["difference"])
    as_of = t.as_of
    run.think(f"Quantity break on {tick} in {acct}: IBOR {fmt_qty(b['ibor_value'])} vs custodian "
              f"{fmt_qty(b['custodian_value'])}, IBOR {'long' if diff > 0 else 'short'} by {fmt_qty(abs(diff))} "
              f"({fmt_money(b['difference_usd'])}). Share counts move for three reasons: trades that haven't "
              "settled (or won't), trades booked differently from what settled, and share events IBOR didn't process.")
    pos = run.call(t.positions(acct, sec))
    books = {r["book"]: r for r in pos.dicts()}
    if len(books) == 1:
        only = next(iter(books))
        run.observe(f"Only the {only.lower()} has a position here; the other book has nothing on file.")
    else:
        run.observe(f"Both books hold it: IBOR {fmt_qty(books['IBOR']['quantity'])}, custodian "
                    f"{fmt_qty(books['CUSTODIAN']['quantity'])}. Pulling recent trades with their settlement status.")
    tr = run.call(t.recent_trades(acct, sec))
    open_ = [r for r in tr.dicts() if r["trade_type"] == "TRADE" and (r["cust_status"] or "NONE") != "SETTLED"]
    ca_rows = [r for r in tr.dicts() if r["trade_type"] == "CA_ADJ"]
    msg = f"{len(tr.rows)} trade{'s' if len(tr.rows) != 1 else ''} since {t.since}"
    if open_:
        msg += "; not fully settled at the custodian: " + ", ".join(
            f"{r['trade_id']} ({(r['cust_status'] or 'no record').lower()}, due {r['settle_date']})" for r in open_[:3])
    else:
        msg += "; every one of them shows SETTLED at the custodian"
    if ca_rows:
        msg += f"; plus {len(ca_rows)} corporate-action adjustment(s) booked by IBOR"
    run.observe(msg + ".")
    fs = run.call(t.foreign_settlements(acct, sec))
    run.observe("No custodian settlements here that IBOR booked to a different account." if not fs.rows else
                f"{len(fs.rows)} custodian settlement(s) landed in {acct} that IBOR booked elsewhere.")
    ca = run.call(t.corporate_actions(acct, sec))
    live_ca = [r for r in ca.dicts() if r["ca_type"] in ("SPLIT", "STOCK_DIV")]
    run.observe("No share events on this line recently." if not live_ca else
                "; ".join(f"{r['ca_id']} {r['ca_type'].replace('_', ' ').lower()} "
                          f"{fmt_ratio(r['ratio_new'], r['ratio_old'])} ex {r['ex_date']}, IBOR {r['ibor_status']}, "
                          f"custodian posted {fmt_qty(r['cust_qty_change'] or 0)}" for r in live_ca) + ".")

    buckets: dict[str, list[tuple[int, int, str]]] = {c: [] for c in CAUSES}  # (row, contribution, note)
    for i, r in enumerate(tr.dicts()):
        if r["trade_type"] == "CA_ADJ":
            continue
        settle = _d(r["settle_date"])
        ibor = _signed(r["side"], r["quantity"])
        if r["cust_account"] is None:
            cause = "TIMING" if settle > as_of else "FAILED_SETTLEMENT"
            contrib = ibor
            note = f"{r['trade_id']} has no custodian instruction at all"
        elif r["cust_account"] != acct:
            cause, contrib = "BOOKING_ERROR", ibor
            note = (f"{r['trade_id']} {r['side']} {fmt_qty(r['quantity'])} sits in {acct} in IBOR but settled into "
                    f"{r['cust_account']} at the custodian")
        else:
            cust = _signed(r["cust_side"], r["settled_quantity"] or 0)
            contrib = ibor - cust
            if contrib == 0:
                continue
            if settle > as_of:
                cause = "TIMING"
                note = (f"{r['trade_id']} {r['side']} {fmt_qty(r['quantity'])} traded {r['trade_date']}, settles "
                        f"{r['settle_date']} (custodian: {r['cust_status']})")
            elif r["cust_side"] != r["side"] or (r["matched_quantity"] is not None
                                                 and int(r["matched_quantity"]) != int(r["quantity"])):
                cause = "BOOKING_ERROR"
                if r["cust_side"] != r["side"]:
                    note = (f"{r['trade_id']} is a {r['side']} in IBOR but the custodian settled a "
                            f"{r['cust_side']} of {fmt_qty(r['settled_quantity'])}")
                else:
                    note = (f"{r['trade_id']} booked {fmt_qty(r['quantity'])} in IBOR, counterparty matched and "
                            f"settled {fmt_qty(r['matched_quantity'])}")
            else:
                cause = "FAILED_SETTLEMENT"
                why = r["fail_reason"] or ("still pending after contractual settlement" if r["cust_status"] == "PENDING"
                                           else r["cust_status"].lower())
                note = (f"{r['trade_id']} {r['side']} {fmt_qty(r['quantity'])} was due {r['settle_date']}; custodian "
                        f"status {r['cust_status']} ({why})"
                        + (f", {fmt_qty(r['settled_quantity'])} delivered" if r["settled_quantity"] else ""))
        buckets[cause].append((i, contrib, note))
    for i, r in enumerate(fs.dicts()):
        if r["settled_quantity"]:
            contrib = -_signed(r["side"], r["settled_quantity"])
            buckets["BOOKING_ERROR"].append((
                1000 + i, contrib,
                f"custodian settled {r['trade_ref']} ({r['side']} {fmt_qty(r['settled_quantity'])}) into {acct}, "
                f"but IBOR has that trade in {r['ibor_account'] or 'no account'}"))
    ca_adj = {}
    for r in tr.dicts():
        if r["trade_type"] == "CA_ADJ" and r["ca_ref"]:
            ca_adj[r["ca_ref"]] = ca_adj.get(r["ca_ref"], 0) + _signed(r["side"], r["quantity"])
    for i, r in enumerate(ca.dicts()):
        if r["ca_type"] not in ("SPLIT", "STOCK_DIV") or _d(r["ex_date"]) > as_of:
            continue
        booked = ca_adj.get(r["ca_id"], 0)
        entitled = int(r["cust_qty_change"] or 0)
        contrib = booked - entitled
        if contrib:
            ratio = fmt_ratio(r["ratio_new"], r["ratio_old"])
            if booked == 0:
                note = (f"{r['ca_id']} {r['ca_type'].replace('_', ' ').lower()} {ratio} went ex {r['ex_date']}; "
                        f"custodian posted {fmt_qty(entitled)} new shares, IBOR status {r['ibor_status']}")
            else:
                note = (f"{r['ca_id']} {ratio} {r['ca_type'].lower()}: IBOR booked {fmt_qty(booked)} shares, "
                        f"custodian entitlement is {fmt_qty(entitled)}")
            buckets["MISSED_CA"].append((2000 + i, contrib, note))

    def qid_row(code: int) -> tuple[str, int]:
        if code >= 2000:
            return ca.qid, code - 2000
        if code >= 1000:
            return fs.qid, code - 1000
        return tr.qid, code

    hyps = []
    for c in CAUSES:
        explained = sum(x[1] for x in buckets[c])
        hyps.append(dict(cause=c, explained=explained, fit=round(_fit(diff, explained), 4),
                         items=len(buckets[c])))
    total = sum(h["explained"] for h in hyps)
    live = [h for h in hyps if h["explained"]]
    for h in live:
        run.observe(f"{LABEL[h['cause']]} explains {fmt_qty(h['explained'])} of {fmt_qty(diff)} shares "
                    f"({h['fit']:.0%} fit, {h['items']} item{'s' if h['items'] != 1 else ''}).")

    exact = [h for h in live if h["explained"] == diff]
    if len(exact) == 1 and len(live) == 1:
        cause, conf = exact[0]["cause"], 0.96
    elif exact:
        cause = exact[0]["cause"]
        conf = 0.8
        run.think(f"{LABEL[cause]} reconciles to the share on its own; the other items net to zero, so I'm treating "
                  "them as noise.")
    elif live and total == diff:
        dom = max(live, key=lambda h: abs(h["explained"]))
        cause, conf = dom["cause"], round(0.55 + 0.35 * abs(dom["explained"]) / sum(abs(h["explained"]) for h in live), 2)
        run.think(f"No single cause covers it, but together they reconcile exactly. {LABEL[cause]} is the biggest piece.")
    elif live:
        dom = max(live, key=lambda h: h["fit"])
        cause, conf = ("UNEXPLAINED", 0.3) if dom["fit"] < 0.98 else (dom["cause"], 0.6)
        run.think(f"Best fit is {LABEL[dom['cause']].lower()} at {dom['fit']:.0%}, which leaves a residual. "
                  "Sending it to a human.")
    else:
        cause, conf = "UNEXPLAINED", 0.2
        run.think("Nothing in the lookback window moves the position. Needs a human to look at older activity.")

    p = pos.dicts()
    ev = [Evidence(pos.qid, i, f"{r['book']} quantity {fmt_qty(r['quantity'])}") for i, r in enumerate(p)]
    items = buckets.get(cause, []) if cause != "UNEXPLAINED" else [x for c in CAUSES for x in buckets[c]]
    for code, contrib, note in items:
        q, row = qid_row(code)
        ev.append(Evidence(q, row, f"{note} ({'+' if contrib > 0 else ''}{fmt_qty(contrib)})"))
    cites = lambda evs: ", ".join(f"{e.qid}:r{e.row}" for e in evs)  # noqa: E731
    pos_ev = ev[:len(p)]
    item_ev = ev[len(p):]
    detail = "; ".join(f"{e.note} [{e.qid}:r{e.row}]" for e in item_ev[:3])

    tr_rows = tr.dicts()
    first = None
    if items and items[0][0] < 1000:
        first = tr_rows[items[0][0]]
    if cause == "TIMING":
        fix = (f"No adjustment. Trade-date vs settle-date timing; should clear on {first['settle_date']}. "
               "Auto-recheck on the next recon run." if first else "No adjustment; recheck next business day.")
    elif cause == "FAILED_SETTLEMENT":
        fix = (f"Chase {first['counterparty']} on {first['trade_id']} ({first['fail_reason'] or 'past due, no status'}). "
               "Keep IBOR position, flag the trade as failing, start buy-in clock if it is still open at T+4."
               if first else "Chase the failing counterparty.")
    elif cause == "BOOKING_ERROR":
        if first and first["cust_account"] and first["cust_account"] != acct:
            fix = f"Re-book {first['trade_id']} from {acct} to {first['cust_account']} (custodian settled it there)."
        elif first and first["cust_side"] != first["side"]:
            fix = f"Amend {first['trade_id']} side {first['side']} -> {first['cust_side']} to match the settled trade."
        elif first:
            fix = (f"Amend {first['trade_id']} quantity {fmt_qty(first['quantity'])} -> "
                   f"{fmt_qty(first['matched_quantity'])} to match the counterparty confirm. Needs 4-eyes.")
        else:
            other = fs.dicts()[0] if fs.rows else None
            fix = (f"Trade {other['trade_ref']} settled into {acct}; IBOR has it in {other['ibor_account']}. "
                   "Re-book to this account." if other else "Correct the booking.")
    elif cause == "MISSED_CA":
        r = ca.dicts()[items[0][0] - 2000] if items else None
        fix = (f"Process {r['ca_id']} ({r['ca_type'].replace('_', ' ').lower()} {fmt_ratio(r['ratio_new'], r['ratio_old'])}) in IBOR "
               f"for {acct}: adjust position by {fmt_qty(-items[0][1])} shares and restate cost."
               if r else "Process the corporate action in IBOR.")
    else:
        fix = "Manual investigation. Playbook could not reconcile the difference from the lookback window."

    if cause == "UNEXPLAINED":
        summary = (f"Could not fully reconcile {fmt_qty(diff)} shares. IBOR {fmt_qty(b['ibor_value'])} vs custodian "
                   f"{fmt_qty(b['custodian_value'])} [{cites(pos_ev)}]. Closest items: {detail or 'none'}.")
    else:
        got = sum(x[1] for x in items)
        tail = (f"That accounts for the full {fmt_qty(diff)} share difference." if got == diff else
                f"That accounts for {fmt_qty(got)} of the {fmt_qty(diff)} share difference.")
        summary = (f"{LABEL[cause]}. IBOR {fmt_qty(b['ibor_value'])} vs custodian "
                   f"{fmt_qty(b['custodian_value'])} [{cites(pos_ev)}]. {detail}. {tail}")
    return Finding(cause, conf, summary, ev, fix, OWNER[cause], hyps)


# ---------------------------------------------------------------------------- MV
def _mv(run: _Run, b: dict, bq: QueryRecord) -> Finding:
    t = run.t
    acct, sec, tick, ccy = b["account_id"], b["security_id"], b["ticker"], b["sec_ccy"]
    diff = float(b["difference"])
    run.think(f"Market value break on {tick} in {acct}: {fmt_money(diff)}. If quantities agree, the gap is price or "
              "FX. I'll split it into a price effect and an FX effect and see which one carries it.")
    pos = run.call(t.positions(acct, sec))
    p = {r["book"]: r for r in pos.dicts()}
    ib, cu = p["IBOR"], p["CUSTODIAN"]
    if ib["quantity"] != cu["quantity"]:
        run.observe("Quantities differ after all; treating as unexplained.")
    q = ib["quantity"]
    price_eff = q * (ib["price"] - cu["price"]) * ib["fx_rate"]
    fx_eff = q * cu["price"] * (ib["fx_rate"] - cu["fx_rate"])
    run.observe(f"Same quantity ({fmt_qty(q)}). Price effect {fmt_money(price_eff)}, FX effect {fmt_money(fx_eff)}, "
                f"sum {fmt_money(price_eff + fx_eff)}.")
    pr = run.call(t.prices(sec))
    prs = pr.dicts()
    as_of_s = t.as_of.isoformat()
    vendor_today = next((i for i, r in enumerate(prs) if r["source"] == "VENDOR" and str(r["price_date"])[:10] == as_of_s), None)
    ibor_rows = [i for i, r in enumerate(prs) if r["source"] == "IBOR"]
    ibor_latest = ibor_rows[0] if ibor_rows else None
    ev_price = []
    price_note = ""
    if ibor_latest is not None:
        r = prs[ibor_latest]
        if str(r["price_date"])[:10] < as_of_s:
            price_note = (f"IBOR golden copy has no {tick} close after {str(r['price_date'])[:10]}; valuation used "
                          f"{r['close_price']} while vendor closed at {cu['price']}")
        else:
            prev_vendor = next((i for i, x in enumerate(prs) if x["source"] == "VENDOR"
                                and str(x["price_date"])[:10] < as_of_s), None)
            if prev_vendor is not None and abs(prs[prev_vendor]["close_price"] - r["close_price"]) < 1e-6 \
                    and abs(r["close_price"] - cu["price"]) > 1e-6:
                price_note = (f"IBOR {as_of_s} close {r['close_price']} is the vendor's prior close "
                              f"({str(prs[prev_vendor]['price_date'])[:10]}), loaded at {r['loaded_at']} before the "
                              "real close was published")
                ev_price.append(Evidence(pr.qid, prev_vendor, "vendor prior close"))
        ev_price.insert(0, Evidence(pr.qid, ibor_latest, "latest IBOR price"))
    if vendor_today is not None:
        ev_price.append(Evidence(pr.qid, vendor_today, "vendor close on the as-of date"))
    if price_note:
        run.observe(price_note + ".")

    ev_fx, fx_note = [], ""
    if ccy != "USD":
        fq = run.call(t.fx(ccy))
        fr = fq.dicts()
        ib_i = next((i for i, r in enumerate(fr) if r["source"] == ib["fx_source"]
                     and str(r["rate_date"])[:10] == str(ib["fx_rate_date"])[:10]), None)
        wmr_i = next((i for i, r in enumerate(fr) if r["source"] == "WMR_4PM" and str(r["rate_date"])[:10] == as_of_s), None)
        if ib["fx_source"] != "WMR_4PM" or str(ib["fx_rate_date"])[:10] != as_of_s:
            fx_note = (f"IBOR valued {ccy} at {ib['fx_rate']} ({ib['fx_source']} {str(ib['fx_rate_date'])[:10]}); "
                       f"custodian used WMR 4pm {as_of_s} at {cu['fx_rate']}")
            run.observe(fx_note + ".")
        if ib_i is not None:
            ev_fx.append(Evidence(fq.qid, ib_i, "rate IBOR used"))
        if wmr_i is not None:
            ev_fx.append(Evidence(fq.qid, wmr_i, "WMR 4pm on the as-of date"))

    hyps = [dict(cause=c, explained=0.0, fit=0.0, items=0) for c in CAUSES]
    for h in hyps:
        if h["cause"] == "STALE_PRICE":
            h.update(explained=round(price_eff, 2), fit=round(_fit(diff, price_eff), 4), items=1 if price_eff else 0)
        if h["cause"] == "FX":
            h.update(explained=round(fx_eff, 2), fit=round(_fit(diff, fx_eff), 4), items=1 if fx_eff else 0)
    tot = abs(price_eff) + abs(fx_eff)
    pos_ev = [Evidence(pos.qid, i, f"{r['book']} position") for i, r in enumerate(pos.dicts())]
    if tot == 0 or abs(price_eff + fx_eff - diff) > max(1.0, abs(diff) * 0.002):
        cause, conf = "UNEXPLAINED", 0.3
    elif abs(price_eff) >= abs(fx_eff):
        cause, conf = "STALE_PRICE", round(0.5 + 0.47 * abs(price_eff) / tot, 2)
    else:
        cause, conf = "FX", round(0.5 + 0.47 * abs(fx_eff) / tot, 2)
    if cause != "UNEXPLAINED" and not (price_note if cause == "STALE_PRICE" else fx_note):
        conf = min(conf, 0.65)  # numbers point one way but I couldn't see why
    secondary = ""
    if cause == "STALE_PRICE" and abs(fx_eff) > 1:
        secondary = f" A smaller FX effect of {fmt_money(fx_eff)} is also in there [{', '.join(f'{e.qid}:r{e.row}' for e in ev_fx)}]."
    if cause == "FX" and abs(price_eff) > 1:
        secondary = f" A smaller price effect of {fmt_money(price_eff)} is also in there."
    if cause == "STALE_PRICE":
        evs = pos_ev + ev_price + (ev_fx if secondary else [])
        summary = (f"Stale price. {price_note or 'IBOR price differs from the vendor close'} "
                   f"[{', '.join(f'{e.qid}:r{e.row}' for e in ev_price)}]. Price effect {fmt_money(price_eff)} on "
                   f"{fmt_qty(q)} shares [{pos.qid}:r0, {pos.qid}:r1].{secondary}")
        fix = (f"Re-price {tick} at the vendor close {cu['price']} for {as_of_s} and re-run the valuation for {acct}. "
               "Raise a ticket on the golden-copy load for this line.")
    elif cause == "FX":
        evs = pos_ev + ev_fx
        summary = (f"FX. {fx_note or 'IBOR and custodian FX rates differ'} "
                   f"[{', '.join(f'{e.qid}:r{e.row}' for e in ev_fx)}]. FX effect {fmt_money(fx_eff)} on local MV "
                   f"{fmt_money(q * cu['price'], ccy)} [{pos.qid}:r0, {pos.qid}:r1].{secondary}")
        fix = (f"Re-run {acct} {ccy} valuation with WMR 4pm {as_of_s} ({cu['fx_rate']}). Check why the batch picked "
               f"{ib['fx_source']} {str(ib['fx_rate_date'])[:10]}.")
    else:
        evs = pos_ev
        summary = (f"Could not split {fmt_money(diff)} into price and FX cleanly [{pos.qid}:r0, {pos.qid}:r1].")
        fix = "Manual investigation."
    return Finding(cause, conf, summary, evs, fix, OWNER[cause], hyps)


# -------------------------------------------------------------------------- CASH
def _cash(run: _Run, b: dict, bq: QueryRecord) -> Finding:
    t = run.t
    acct, ccy = b["account_id"], b["currency"]
    fund = acct.removesuffix("-INC")
    diff = float(b["difference"])
    run.think(f"Cash break on income account {acct} ({ccy}): IBOR {fmt_money(b['ibor_value'], ccy)} vs custodian "
              f"{fmt_money(b['custodian_value'], ccy)}. Income cash only moves on dividends and coupons, so I'll look "
              "for entries that exist on one side only.")
    bal = run.call(t.cash_balances(acct, ccy))
    run.observe(f"Confirmed: custodian is {'above' if diff < 0 else 'below'} IBOR by {fmt_money(abs(diff), ccy)}. "
                "Looking for entries that only one book has.")
    um = run.call(t.unmatched_cash(acct, ccy))
    rows = um.dicts()
    explained = sum(-r["amount"] if r["side"] == "CUSTODIAN_ONLY" else r["amount"] for r in rows)
    refs = sorted({r["reference"] for r in rows if r["reference"]})
    run.observe(f"{len(rows)} one-sided entr{'y' if len(rows) == 1 else 'ies'} totalling {fmt_money(explained, ccy)} "
                f"against a break of {fmt_money(diff, ccy)}.")
    cad = run.call(t.ca_detail(refs, fund)) if refs else None
    hyps = [dict(cause=c, explained=0.0, fit=0.0, items=0) for c in CAUSES]
    divs = [i for i, r in enumerate(rows) if r["entry_type"] == "DIVIDEND"]
    for h in hyps:
        if h["cause"] == "MISSED_CA":
            amt = sum(-rows[i]["amount"] if rows[i]["side"] == "CUSTODIAN_ONLY" else rows[i]["amount"] for i in divs)
            h.update(explained=round(amt, 2), fit=round(_fit(diff, amt), 4), items=len(divs))
    ev = [Evidence(bal.qid, i, f"{r['book']} balance") for i, r in enumerate(bal.dicts())]
    ev += [Evidence(um.qid, i, f"{rows[i]['side'].lower().replace('_', ' ')} {rows[i]['entry_type'].lower()} "
                               f"{rows[i]['reference']} {fmt_money(rows[i]['amount'], ccy)}") for i in divs]
    if cad:
        ev += [Evidence(cad.qid, i, f"{r['ca_id']} {r['ticker']} dividend, IBOR {r['ibor_status']}")
               for i, r in enumerate(cad.dicts())]
    if divs and abs(explained - diff) < 0.01:
        cad_rows = cad.dicts() if cad else []
        names = ", ".join(f"{r['ticker']} {r['ca_id']} paid {r['pay_date']} ({fmt_money(r['cust_cash'] or 0, ccy)}, IBOR "
                          f"{r['ibor_status']})" for r in cad_rows)
        cite_um = ", ".join(f"{um.qid}:r{i}" for i in divs)
        cite_ca = ", ".join(f"{cad.qid}:r{i}" for i in range(len(cad_rows))) if cad else ""
        summary = (f"Missed corporate action. Custodian credited {len(divs)} dividend"
                   f"{'s' if len(divs) > 1 else ''} IBOR never booked [{cite_um}]: {names} [{cite_ca}]. "
                   f"Together they account for the full {fmt_money(diff, ccy)} [{bal.qid}:r0, {bal.qid}:r1].")
        fix = (f"Book the missing dividend income in IBOR for {fund}: "
               + ", ".join(f"{r['ca_id']} {fmt_money(r['cust_cash'] or 0, ccy)}" for r in cad_rows)
               + ". Check why the CA feed skipped this account.")
        return Finding("MISSED_CA", 0.95 if len(divs) == 1 else 0.9, summary, ev, fix, OWNER["MISSED_CA"], hyps)
    summary = (f"Could not tie {fmt_money(diff, ccy)} to one-sided entries ({fmt_money(explained, ccy)} found) "
               f"[{bal.qid}:r0, {bal.qid}:r1].")
    return Finding("UNEXPLAINED", 0.25, summary, ev, "Manual investigation.", OWNER["UNEXPLAINED"], hyps)


def investigate(break_id: str, tools: ReconTools) -> Investigation:
    run = _Run(tools, break_id)
    bq = run.call(tools.break_detail(break_id))
    if bq.error or not bq.rows:
        f = Finding("UNEXPLAINED", 0.0, f"Break {break_id} not found [{bq.qid}].", [], "Check the break id.",
                    OWNER["UNEXPLAINED"])
        return Investigation(break_id, run.steps, f)
    b = bq.dicts()[0]
    handler = {"QTY": _qty, "MV": _mv, "CASH": _cash}[b["break_type"]]
    finding = handler(run, b, bq)
    finding.evidence.insert(0, Evidence(bq.qid, 0, "break record"))
    run.think(f"Conclusion: {LABEL[finding.root_cause].lower()}, {finding.confidence:.0%} confidence. "
              f"Owner: {finding.owner_team}.")
    return Investigation(break_id, run.steps, finding)
