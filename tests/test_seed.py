from collections import Counter

from breakbot.seed import ROOT_CAUSES, SECURITIES, isin_ok


def test_isins_have_valid_check_digits():
    assert all(isin_ok(s[1]) for s in SECURITIES)
    assert not isin_ok("US0378331006")


def test_two_hundred_breaks_all_labelled(world):
    breaks, labels = world["breaks"], world["break_labels"]
    assert len(breaks) == 200
    assert set(breaks.break_id) == set(labels.break_id)
    counts = Counter(labels.root_cause)
    assert set(counts) == set(ROOT_CAUSES)
    assert min(counts.values()) >= 25


def test_breaks_are_real_recon_differences(world):
    """Every QTY break must equal IBOR minus custodian position."""
    ib = world["ibor_positions"].set_index(["account_id", "security_id"]).quantity
    cu = world["custodian_positions"].set_index(["account_id", "security_id"]).quantity
    for b in world["breaks"][world["breaks"].break_type == "QTY"].itertuples():
        key = (b.account_id, int(b.security_id))
        assert ib.get(key, 0) - cu.get(key, 0) == b.difference


def test_deterministic():
    from breakbot.seed import build_world
    a = build_world(seed=11)["breaks"].difference.round(2).tolist()
    b = build_world(seed=11)["breaks"].difference.round(2).tolist()
    assert a == b
