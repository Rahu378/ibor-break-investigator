"""The Claude loop, driven by a scripted fake client (no network)."""
from types import SimpleNamespace as NS

from breakbot import llm
from breakbot.tools import ReconTools


class FakeMessages:
    def __init__(self, script):
        self.script = script
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return self.script.pop(0)


def tool_use(id_, name, inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


def test_loop_runs_tools_then_submits(ro_engine, labels):
    bid = sorted(labels)[0]
    script = [
        NS(stop_reason="tool_use", content=[
            NS(type="text", text="Checking positions first."),
            tool_use("t1", "run_sql", {"sql": "SELECT account_id, security_id FROM breaks WHERE break_id = '%s'" % bid,
                                       "purpose": "re-read the break"}),
        ]),
        NS(stop_reason="tool_use", content=[
            tool_use("t2", "submit_finding", {
                "root_cause": "TIMING", "confidence": 0.9, "summary": "Pending trade [Q2:r0].",
                "evidence": [{"query_id": "Q2", "row": 0, "note": "break"}],
                "proposed_fix": "none", "owner_team": ""}),
        ]),
    ]
    fake = NS(beta=NS(messages=FakeMessages(script)))
    tools = ReconTools(ro_engine)
    inv = llm.investigate(bid, tools, client=fake)
    assert inv.finding.root_cause == "TIMING"
    assert inv.finding.owner_team  # filled from the owner map
    assert [q.qid for q in tools.queries] == ["Q1", "Q2"]
    second = fake.beta.messages.calls[1]["messages"]
    # assistant turn is echoed back untouched, tool results come back in one user turn
    assert second[1]["role"] == "assistant" and second[2]["role"] == "user"
    assert second[2]["content"][0]["tool_use_id"] == "t1"
    assert fake.beta.messages.calls[0]["output_config"] == {"effort": "medium"}


def test_guard_errors_go_back_to_the_model(ro_engine, labels):
    bid = sorted(labels)[0]
    script = [
        NS(stop_reason="tool_use", content=[tool_use("t1", "run_sql", {"sql": "SELECT * FROM break_labels",
                                                                        "purpose": "cheat"})]),
        NS(stop_reason="tool_use", content=[tool_use("t2", "submit_finding", {
            "root_cause": "UNEXPLAINED", "confidence": 0.1, "summary": "n/a", "evidence": [],
            "proposed_fix": "", "owner_team": "Reconciliations"})]),
    ]
    fake = NS(beta=NS(messages=FakeMessages(script)))
    llm.investigate(bid, ReconTools(ro_engine), client=fake)
    result = fake.beta.messages.calls[1]["messages"][2]["content"][0]
    assert result["is_error"] and "allow-list" in result["content"]
