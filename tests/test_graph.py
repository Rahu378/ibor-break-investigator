import pytest

from breakbot.graph import build_graph, resume, start


@pytest.fixture
def graph(ro_engine, audit):
    return build_graph(engine=ro_engine, audit=audit)


def first_with(labels, cause):
    return next(b for b, c in sorted(labels.items()) if c == cause)



def test_parks_at_human_review(graph, easy_labels):
    cfg, st = start(graph, first_with(easy_labels, "FAILED_SETTLEMENT"))
    assert st["status"] == "AWAITING_REVIEW"
    assert graph.get_state(cfg).next == ("human_review",)


def test_approve_assigns_owner(graph, easy_labels, audit):
    bid = first_with(easy_labels, "BOOKING_ERROR")
    cfg, st = start(graph, bid)
    st = resume(graph, cfg, {"action": "approve", "reviewer": "rr"})
    assert st["status"] == "ASSIGNED:Trade Support"
    review = audit.reviews()[0]
    assert review["action"] == "approve" and review["final_root_cause"] == "BOOKING_ERROR"


def test_timing_closes_without_action(graph, easy_labels):
    cfg, _ = start(graph, first_with(easy_labels, "TIMING"))
    assert resume(graph, cfg, {"action": "approve", "reviewer": "rr"})["status"] == "CLOSED_NO_ACTION"


def test_edit_overrides_agent(graph, easy_labels, audit):
    cfg, _ = start(graph, first_with(easy_labels, "FX"))
    st = resume(graph, cfg, {"action": "edit", "reviewer": "rr", "root_cause": "STALE_PRICE",
                             "owner_team": "Pricing & Valuations", "note": "vendor issue"})
    assert st["review"]["root_cause"] == "STALE_PRICE"
    assert audit.reviews()[0]["proposed_root_cause"] == "FX"


def test_reject_reopens(graph, easy_labels):
    cfg, _ = start(graph, first_with(easy_labels, "MISSED_CA"))
    assert resume(graph, cfg, {"action": "reject", "reviewer": "rr"})["status"] == "REOPENED_MANUAL"


def test_bad_action_is_refused(graph, easy_labels):
    cfg, _ = start(graph, first_with(easy_labels, "TIMING"))
    with pytest.raises(ValueError):
        resume(graph, cfg, {"action": "close it", "reviewer": "rr"})


def test_every_step_is_audited(graph, easy_labels, audit):
    bid = first_with(easy_labels, "STALE_PRICE")
    cfg, st = start(graph, bid)
    resume(graph, cfg, {"action": "approve", "reviewer": "rr"})
    nodes = [a["node"] for a in reversed(audit.trail(bid))]
    for n in ("load_break", "tool", "investigate", "check_citations", "propose_fix", "human_review", "close"):
        assert n in nodes
    assert sum(1 for n in nodes if n == "tool") == len(st["queries"])
