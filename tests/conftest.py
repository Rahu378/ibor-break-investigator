import pytest
from sqlalchemy import create_engine, text

from breakbot import seed
from breakbot.audit import Audit
from breakbot.tools import read_only_engine


@pytest.fixture(scope="session")
def world():
    return seed.build_world(seed=7)


@pytest.fixture(scope="session")
def db_url(world, tmp_path_factory):
    path = tmp_path_factory.mktemp("recon") / "recon.db"
    url = f"sqlite:///{path}"
    seed.write(world, url)
    return url


@pytest.fixture(scope="session")
def ro_engine(db_url):
    return read_only_engine(db_url)


@pytest.fixture(scope="session")
def labels(db_url):
    with create_engine(db_url).connect() as c:
        return {r.break_id: r.root_cause for r in c.execute(text("SELECT break_id, root_cause FROM break_labels"))}


@pytest.fixture(scope="session")
def easy_labels(db_url):
    """Labels minus the deliberately hard variants the playbook is expected to abstain on."""
    hard = ("aged_", "wrong_security", "gross_paid_net")
    with create_engine(db_url).connect() as c:
        rows = c.execute(text("SELECT break_id, root_cause, variant FROM break_labels"))
        return {r.break_id: r.root_cause for r in rows if not any(h in r.variant for h in hard)}


@pytest.fixture
def audit(tmp_path):
    return Audit(f"sqlite:///{tmp_path / 'audit.db'}")
