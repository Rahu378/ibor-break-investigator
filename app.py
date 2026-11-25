"""Analyst review queue.

    streamlit run app.py

Pick a break, let the agent investigate, then approve / edit / reject the
proposed root cause, fix and owner. Nothing closes without a decision here.
"""
from __future__ import annotations

import json
import re

import pandas as pd
import streamlit as st
from sqlalchemy import text

from breakbot import llm
from breakbot.audit import Audit
from breakbot.config import AS_OF, ROOT
from breakbot.graph import build_graph, resume, start
from breakbot.playbook import CAUSES, LABEL, OWNER
from breakbot.tools import read_only_engine

st.set_page_config(page_title="Break Investigator", layout="wide")


@st.cache_resource
def _graph():
    # one graph (and one in-memory checkpointer) per server process, so parked runs survive reruns
    return build_graph()


@st.cache_resource
def _engine():
    return read_only_engine()


@st.cache_data(ttl=30)
def _queue() -> pd.DataFrame:
    with _engine().connect() as conn:
        return pd.read_sql(text("""
            SELECT b.break_id, b.account_id, s.ticker, b.break_type, b.currency, b.ibor_value, b.custodian_value,
                   b.difference, b.difference_usd
            FROM breaks b LEFT JOIN securities s ON s.security_id = b.security_id
            ORDER BY ABS(b.difference_usd) DESC"""), conn)


def _cite_md(summary: str) -> str:
    return re.sub(r"\[([^\]]*Q\d+:r\d+[^\]]*)\]", lambda m: f" `{m.group(1)}`", summary)


graph = _graph()
audit = Audit()
runs: dict = st.session_state.setdefault("runs", {})

st.title("Break Investigator")
st.caption(f"IBOR vs custodian · as of {AS_OF:%a %d %b %Y}")

with st.sidebar:
    modes = ["playbook"] + (["llm"] if llm.available() else [])
    mode = st.radio("Investigator", modes, help="llm needs ANTHROPIC_API_KEY")
    reviewer = st.text_input("Reviewer", value="analyst")
    q = _queue()
    types = st.multiselect("Break type", ["QTY", "MV", "CASH"], default=["QTY", "MV", "CASH"])
    accts = st.multiselect("Account", sorted(q.account_id.unique()))

tab_q, tab_audit, tab_bench = st.tabs(["Queue", "Audit trail", "Benchmark"])

with tab_q:
    view = q[q.break_type.isin(types)]
    if accts:
        view = view[view.account_id.isin(accts)]
    left, right = st.columns([0.42, 0.58], gap="large")
    with left:
        st.dataframe(view, hide_index=True, width="stretch", height=560,
                     column_config={"difference_usd": st.column_config.NumberColumn("diff USD", format="%.0f")})
        bid = st.selectbox("Break", view.break_id.tolist())
        if st.button("Investigate", type="primary", disabled=not bid):
            with st.spinner(f"investigating {bid}…"):
                cfg, state = start(graph, bid, mode=mode)
            runs[bid] = dict(cfg=cfg, state=state)

    with right:
        run = runs.get(bid)
        if not run:
            st.info("Pick a break and hit Investigate.")
        else:
            state = run["state"]
            f = state["finding"]
            c1, c2, c3 = st.columns(3)
            c1.metric("Root cause", LABEL.get(f["root_cause"], f["root_cause"]))
            c2.metric("Confidence", f"{f['confidence']:.0%}")
            c3.metric("Queries", len(state["queries"]))
            if state.get("needs_attention"):
                st.warning("Flagged: " + ", ".join(state["needs_attention"]))
            st.markdown(_cite_md(f["summary"]))
            with st.expander("Investigation steps", expanded=False):
                for s in state["steps"]:
                    if s["kind"] == "tool":
                        st.markdown(f"**{s['qid']}** · `{s['tool']}` — {s['text']}")
                    elif s["kind"] == "thought":
                        st.markdown(f"_{s['text']}_")
                    else:
                        st.markdown(f"→ {s['text']}")
            for qr in state["queries"]:
                with st.expander(f"{qr['qid']} · {qr['tool']} · {qr['row_count']} rows"):
                    st.code(qr["sql"], language="sql")
                    if qr.get("error"):
                        st.error(qr["error"])
                    elif qr["rows"]:
                        st.dataframe(pd.DataFrame(qr["rows"], columns=qr["columns"]), width="stretch")

            st.subheader("Review")
            if state.get("review"):
                r = state["review"]
                st.success(f"{r['action'].upper()} by {r['reviewer']} → {state['status']}")
            else:
                with st.form(f"review-{bid}"):
                    action = st.radio("Decision", ["approve", "edit", "reject"], horizontal=True)
                    cause = st.selectbox("Root cause", list(CAUSES) + ["UNEXPLAINED"],
                                         index=(list(CAUSES) + ["UNEXPLAINED"]).index(f["root_cause"]))
                    fix = st.text_area("Fix", value=f["proposed_fix"])
                    teams = sorted(set(OWNER.values()))
                    owner = st.selectbox("Owner team", teams, index=teams.index(f["owner_team"])
                                         if f["owner_team"] in teams else 0)
                    note = st.text_input("Note")
                    if st.form_submit_button("Submit decision"):
                        decision = dict(action=action, reviewer=reviewer, note=note)
                        if action == "edit":
                            decision.update(root_cause=cause, proposed_fix=fix, owner_team=owner)
                        run["state"] = resume(graph, run["cfg"], decision)
                        st.rerun()

with tab_audit:
    trail = audit.trail(limit=1000)
    if trail:
        df = pd.DataFrame(trail)
        df["payload"] = df.payload.map(lambda p: json.dumps(json.loads(p))[:160] if isinstance(p, str) else "")
        st.dataframe(df, hide_index=True, width="stretch", height=600)
    else:
        st.caption("Nothing logged yet.")

with tab_bench:
    reports = sorted((ROOT / "reports").glob("benchmark_*.json"))
    if not reports:
        st.caption("Run `python -m breakbot.benchmark` first.")
    for p in reports:
        m = json.loads(p.read_text())
        st.subheader(f"{m['mode']} · {m['generated_at']}")
        a, b, c, d = st.columns(4)
        a.metric("Matched labels", f"{m['accuracy']:.1%}", f"{m['correct']}/{m['n']}")
        b.metric("Abstained", m["abstained"])
        c.metric("Misclassified", m["misclassified"])
        d.metric("Cited evidence", f"{m['citation_rate']:.0%}")
        cm = pd.DataFrame(m["confusion"]).T.fillna(0).astype(int)
        top = max(1, int(cm.values.max()))
        st.dataframe(cm.style.map(lambda v: f"background-color: rgba(88, 86, 214, {0.12 + 0.6 * v / top:.2f})" if v else ""),
                     width="stretch")
