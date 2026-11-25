"""Claude-driven investigator.

Same tools as the playbook, but the model decides what to query. It can write
its own SELECTs (still guarded), so it can look past the playbook's fixed
lookback window, join settlements on trade_ref to spot a trade that settled
against another security, or compare dividend amounts across books.

Notes:
  * the model has to finish by calling `submit_finding`. Opus 5.5 doesn't take
    a forced tool_choice, so the system prompt says so and `strict` keeps the
    payload schema-valid.
  * assistant content is appended back untouched (thinking blocks included).
  * refusals fall back server-side, and I stop on `refusal` instead of reading
    half a turn.
"""
from __future__ import annotations

import json
import os

from .config import EFFORT, MODEL
from .playbook import CAUSES, LABEL, OWNER, Evidence, Finding, Investigation
from .tools import ReconTools

MAX_TURNS = 14

SYSTEM = """You investigate reconciliation breaks between an asset manager's IBOR (investment book of record) and
its custodian. You have read-only SQL access to a recon warehouse.

How the books work:
- IBOR books positions on trade date. The custodian books on settlement (settled_quantity, settled_on).
- US/CA settle T+1, everything else T+2 (no holiday calendar).
- IBOR processes corporate actions itself: share events become trades with trade_type='CA_ADJ' and ca_ref set,
  dividends land in ibor_cash_activity on the income account (<fund>-INC). The custodian's side of the same events
  is custodian_ca_postings and custodian_cash_activity.
- IBOR prices come from prices where source='IBOR'; the custodian uses source='VENDOR'. IBOR FX for each position
  is in ibor_positions.fx_source / fx_rate_date; the custodian always uses WMR_4PM on the as-of date.

Root causes (pick exactly one):
TIMING            - traded, not yet due to settle; will clear on its own
MISSED_CA         - corporate action missed or mis-processed in IBOR (split, stock dividend, cash dividend)
STALE_PRICE       - IBOR valued with an old or wrong price
FX                - IBOR valued with the wrong FX fixing
FAILED_SETTLEMENT - due to settle but has not (failed, partial, or past due)
BOOKING_ERROR     - IBOR booking differs from what settled (qty, side, account or security)
UNEXPLAINED       - only if the evidence doesn't reconcile; say what's missing

Rules:
- Quantify. A root cause must explain the break amount, not just be present.
- Every factual claim in the summary cites the query and row it came from, as [Q3:r0]. Row numbers are the r-indexes
  shown in tool results. Do not cite rows you have not seen.
- Prefer the typed helper tools; use run_sql for anything they don't cover.
- When you are done, call submit_finding exactly once. Do not answer in plain text."""


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


TOOLS = [
    {
        "name": "run_sql",
        "description": "Run one read-only SELECT against the recon warehouse (SQLite/Postgres syntax, ISO date "
                       "strings). Only allow-listed tables; a LIMIT is added. Returns rows as r0, r1, ... under a "
                       "query id you can cite.",
        "input_schema": _schema({"sql": {"type": "string"}, "purpose": {"type": "string"}}),
        "strict": True,
    },
    {
        "name": "positions",
        "description": "IBOR and custodian position rows (qty, price, price_date, fx_rate, fx_source) for an account "
                       "and security.",
        "input_schema": _schema({"account_id": {"type": "string"}, "security_id": {"type": "integer"}}),
        "strict": True,
    },
    {
        "name": "recent_trades",
        "description": "IBOR trades for an account/security over the last ~10 business days joined to custodian "
                       "settlement status on trade_ref.",
        "input_schema": _schema({"account_id": {"type": "string"}, "security_id": {"type": "integer"}}),
        "strict": True,
    },
    {
        "name": "corporate_actions",
        "description": "Recent corporate actions on a security with IBOR processing status and custodian postings "
                       "for one fund account.",
        "input_schema": _schema({"account_id": {"type": "string"}, "security_id": {"type": "integer"}}),
        "strict": True,
    },
    {
        "name": "prices",
        "description": "IBOR vs VENDOR closes for a security over the last week.",
        "input_schema": _schema({"security_id": {"type": "integer"}}),
        "strict": True,
    },
    {
        "name": "fx",
        "description": "WMR_4PM and INTRADAY_10AM fixings to USD for a currency over the last few days.",
        "input_schema": _schema({"currency": {"type": "string"}}),
        "strict": True,
    },
    {
        "name": "unmatched_cash",
        "description": "Income-cash entries that exist on only one book, for an income account (e.g. GEQ-001-INC).",
        "input_schema": _schema({"account_id": {"type": "string"}, "currency": {"type": "string"}}),
        "strict": True,
    },
    {
        "name": "submit_finding",
        "description": "Submit the root cause. Call once, at the end.",
        "input_schema": _schema({
            "root_cause": {"type": "string", "enum": list(CAUSES) + ["UNEXPLAINED"]},
            "confidence": {"type": "number"},
            "summary": {"type": "string", "description": "2-4 sentences, every claim cited like [Q3:r1]"},
            "evidence": {"type": "array", "items": _schema({
                "query_id": {"type": "string"}, "row": {"type": "integer"}, "note": {"type": "string"}})},
            "proposed_fix": {"type": "string"},
            "owner_team": {"type": "string"},
        }),
        "strict": True,
    },
]


def _dispatch(tools: ReconTools, name: str, args: dict):
    if name == "run_sql":
        return tools.run_sql(args["sql"], args.get("purpose", ""))
    if name == "positions":
        return tools.positions(args["account_id"], int(args["security_id"]))
    if name == "recent_trades":
        return tools.recent_trades(args["account_id"], int(args["security_id"]))
    if name == "corporate_actions":
        return tools.corporate_actions(args["account_id"], int(args["security_id"]))
    if name == "prices":
        return tools.prices(int(args["security_id"]))
    if name == "fx":
        return tools.fx(args["currency"])
    if name == "unmatched_cash":
        return tools.unmatched_cash(args["account_id"], args["currency"])
    raise KeyError(name)


def investigate(break_id: str, tools: ReconTools, client=None, model: str = MODEL) -> Investigation:
    import anthropic

    client = client or anthropic.Anthropic()
    steps: list[dict] = []
    first = tools.break_detail(break_id)
    steps.append(dict(kind="tool", qid=first.qid, tool=first.tool, text=first.purpose))
    user = (f"Investigate break {break_id}. As-of date {tools.as_of}.\n\nBreak record:\n{first.for_model()}\n\n"
            f"Tables you can query:\n{tools.schema()}")
    messages = [{"role": "user", "content": user}]

    for _ in range(MAX_TURNS):
        resp = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
            output_config={"effort": EFFORT},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"model declined: {getattr(resp, 'stop_details', None)}")
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        submitted = None
        for block in resp.content:
            if block.type == "text" and block.text.strip():
                steps.append(dict(kind="thought", text=block.text.strip()))
            if block.type != "tool_use":
                continue
            args = block.input if isinstance(block.input, dict) else json.loads(block.input)
            if block.name == "submit_finding":
                submitted = args
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": "recorded"})
                continue
            try:
                rec = _dispatch(tools, block.name, args)
                steps.append(dict(kind="tool", qid=rec.qid, tool=rec.tool, text=args.get("purpose") or rec.purpose))
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": rec.for_model(),
                                "is_error": bool(rec.error)})
            except Exception as e:  # bad args etc. go back to the model
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": f"error: {e}",
                                "is_error": True})
        if submitted:
            cause = submitted["root_cause"]
            finding = Finding(
                root_cause=cause,
                confidence=float(max(0.0, min(1.0, submitted["confidence"]))),
                summary=submitted["summary"],
                evidence=[Evidence(e["query_id"], int(e["row"]), e["note"]) for e in submitted["evidence"]],
                proposed_fix=submitted["proposed_fix"],
                owner_team=submitted["owner_team"] or OWNER.get(cause, "Reconciliations"),
            )
            steps.append(dict(kind="thought", text=f"Conclusion: {LABEL[cause].lower()}, "
                                                   f"{finding.confidence:.0%} confidence. Owner: {finding.owner_team}."))
            return Investigation(break_id, steps, finding, mode="llm")
        if not results:
            messages.append({"role": "user", "content": "Call submit_finding with your conclusion."})
        else:
            messages.append({"role": "user", "content": results})
    finding = Finding("UNEXPLAINED", 0.0, "Agent ran out of turns.", [], "Manual investigation.",
                      OWNER["UNEXPLAINED"])
    return Investigation(break_id, steps, finding, mode="llm")


def available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
