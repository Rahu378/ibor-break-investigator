import re

from breakbot.playbook import investigate
from breakbot.tools import ReconTools


def test_playbook_never_guesses_wrong(ro_engine, labels):
    """It may abstain (UNEXPLAINED), but it should never confidently pick the wrong cause."""
    correct = wrong = 0
    for bid, truth in labels.items():
        f = investigate(bid, ReconTools(ro_engine)).finding
        if f.root_cause == truth:
            correct += 1
        elif f.root_cause != "UNEXPLAINED":
            wrong += 1
    assert wrong == 0
    assert correct / len(labels) >= 0.95


def test_every_citation_points_at_a_real_row(ro_engine, labels):
    for bid in list(labels)[:60]:
        tools = ReconTools(ro_engine)
        f = investigate(bid, tools).finding
        rows = {q.qid: len(q.rows) for q in tools.queries}
        cites = re.findall(r"(Q\d+):r(\d+)", f.summary)
        assert cites, bid
        for qid, r in cites:
            assert int(r) < rows[qid], (bid, qid, r)
        for e in f.evidence:
            assert e.row < rows[e.qid]
