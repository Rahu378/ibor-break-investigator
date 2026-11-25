"""The investigation workflow as a LangGraph state machine.

    load_break -> investigate -> check_citations -> propose_fix -> human_review -> close
                                                                       |
                                                    (reject) ----------+--> reopen

`human_review` calls interrupt(), so the run parks there with its state in the
checkpointer until an analyst resumes it with a decision. Nothing closes on the
agent's say-so.

Every node writes to the audit trail.
"""
from __future__ import annotations

import uuid
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import llm, playbook
from .audit import Audit
from .playbook import OWNER
from .tools import ReconTools, read_only_engine

VALID_ACTIONS = {"approve", "edit", "reject"}
REVIEW_THRESHOLD = 0.7


class BreakState(TypedDict, total=False):
    run_id: str
    break_id: str
    mode: str
    break_row: dict
    steps: list[dict]
    queries: list[dict]
    finding: dict
    citation_check: dict
    needs_attention: list[str]
    review: dict
    status: str


def build_graph(engine=None, audit: Audit | None = None, checkpointer=None, llm_client=None):
    engine = engine or read_only_engine()
    audit = audit or Audit()
    tools_by_run: dict[str, ReconTools] = {}

    def _tools(state) -> ReconTools:
        rid = state["run_id"]
        if rid not in tools_by_run:
            tools_by_run[rid] = ReconTools(engine, on_query=lambda q: audit.log(
                rid, state["break_id"], "tool", "query", dict(qid=q.qid, tool=q.tool, sql=q.sql,
                                                               rows=len(q.rows), error=q.error)))
        return tools_by_run[rid]

    def load_break(state: BreakState) -> dict:
        t = ReconTools(engine)
        rec = t.break_detail(state["break_id"])
        row = rec.dicts()[0] if rec.rows else {}
        audit.log(state["run_id"], state["break_id"], "load_break", "loaded", row)
        return {"break_row": row, "status": "INVESTIGATING" if row else "NOT_FOUND"}

    def investigate(state: BreakState) -> dict:
        tools = _tools(state)
        mode = state.get("mode", "playbook")
        if mode == "llm":
            inv = llm.investigate(state["break_id"], tools, client=llm_client)
        else:
            inv = playbook.investigate(state["break_id"], tools)
        audit.log(state["run_id"], state["break_id"], "investigate", "finding",
                  dict(mode=mode, root_cause=inv.finding.root_cause, confidence=inv.finding.confidence))
        return {"steps": inv.steps, "finding": inv.finding.as_dict(),
                "queries": [q.as_dict() for q in tools.queries]}

    def check_citations(state: BreakState) -> dict:
        """Every evidence item and every [Qn:rm] in the summary must point at a real row."""
        import re

        qs = {q["qid"]: q for q in state["queries"]}
        f = state["finding"]
        bad = []
        for e in f["evidence"]:
            q = qs.get(e["qid"])
            if q is None or q.get("error") or e["row"] >= q["row_count"]:
                bad.append(f"{e['qid']}:r{e['row']}")
        inline = re.findall(r"(Q\d+):r(\d+)", f["summary"])
        for qid, row in inline:
            q = qs.get(qid)
            if q is None or int(row) >= q["row_count"]:
                bad.append(f"{qid}:r{row}")
        check = dict(evidence_items=len(f["evidence"]), inline_citations=len(inline), dangling=sorted(set(bad)),
                     ok=bool(f["evidence"]) and bool(inline) and not bad)
        flags = []
        if not check["ok"]:
            flags.append("citations")
        if f["confidence"] < REVIEW_THRESHOLD:
            flags.append("low_confidence")
        if f["root_cause"] == "UNEXPLAINED":
            flags.append("unexplained")
        audit.log(state["run_id"], state["break_id"], "check_citations", "checked", check)
        return {"citation_check": check, "needs_attention": flags}

    def propose_fix(state: BreakState) -> dict:
        f = dict(state["finding"])
        if not f.get("owner_team"):
            f["owner_team"] = OWNER.get(f["root_cause"], "Reconciliations")
        audit.log(state["run_id"], state["break_id"], "propose_fix", "proposed",
                  dict(fix=f["proposed_fix"], owner=f["owner_team"]))
        return {"finding": f, "status": "AWAITING_REVIEW"}

    def human_review(state: BreakState) -> dict:
        f = state["finding"]
        decision = interrupt({
            "break_id": state["break_id"],
            "root_cause": f["root_cause"],
            "confidence": f["confidence"],
            "summary": f["summary"],
            "proposed_fix": f["proposed_fix"],
            "owner_team": f["owner_team"],
            "needs_attention": state.get("needs_attention", []),
        })
        action = str(decision.get("action", "")).lower()
        if action not in VALID_ACTIONS:
            raise ValueError(f"review action must be one of {sorted(VALID_ACTIONS)}")
        reviewer = decision.get("reviewer") or "unknown"
        final = dict(
            root_cause=decision.get("root_cause") or f["root_cause"],
            proposed_fix=decision.get("proposed_fix") or f["proposed_fix"],
            owner_team=decision.get("owner_team") or f["owner_team"],
        )
        review = dict(action=action, reviewer=reviewer, note=decision.get("note", ""), **final)
        audit.log(state["run_id"], state["break_id"], "human_review", action, review, actor=reviewer)
        audit.record_review(state["run_id"], state["break_id"], reviewer, action, f["root_cause"],
                            None if action == "reject" else final["root_cause"],
                            None if action == "reject" else final["proposed_fix"],
                            None if action == "reject" else final["owner_team"], review["note"])
        return {"review": review}

    def close(state: BreakState) -> dict:
        r = state["review"]
        status = "CLOSED_NO_ACTION" if r["root_cause"] == "TIMING" else f"ASSIGNED:{r['owner_team']}"
        audit.log(state["run_id"], state["break_id"], "close", status, actor=r["reviewer"])
        return {"status": status}

    def reopen(state: BreakState) -> dict:
        audit.log(state["run_id"], state["break_id"], "reopen", "rejected_by_analyst", state["review"],
                  actor=state["review"]["reviewer"])
        return {"status": "REOPENED_MANUAL"}

    g = StateGraph(BreakState)
    g.add_node("load_break", load_break)
    g.add_node("investigate", investigate)
    g.add_node("check_citations", check_citations)
    g.add_node("propose_fix", propose_fix)
    g.add_node("human_review", human_review)
    g.add_node("close", close)
    g.add_node("reopen", reopen)
    g.add_edge(START, "load_break")
    g.add_conditional_edges("load_break", lambda s: "investigate" if s.get("break_row") else END,
                            {"investigate": "investigate", END: END})
    g.add_edge("investigate", "check_citations")
    g.add_edge("check_citations", "propose_fix")
    g.add_edge("propose_fix", "human_review")
    g.add_conditional_edges("human_review", lambda s: "reopen" if s["review"]["action"] == "reject" else "close",
                            {"reopen": "reopen", "close": "close"})
    g.add_edge("close", END)
    g.add_edge("reopen", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())


def start(graph, break_id: str, mode: str = "playbook") -> tuple[dict, dict]:
    """Run until the human-review interrupt. Returns (config, state)."""
    run_id = f"run-{uuid.uuid4().hex[:10]}"
    cfg = {"configurable": {"thread_id": run_id}}
    graph.invoke({"run_id": run_id, "break_id": break_id, "mode": mode}, cfg)
    return cfg, graph.get_state(cfg).values


def resume(graph, cfg: dict, decision: dict[str, Any]) -> dict:
    graph.invoke(Command(resume=decision), cfg)
    return graph.get_state(cfg).values
