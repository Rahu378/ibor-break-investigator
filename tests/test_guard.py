import pytest

from breakbot.tools import GuardError, ReconTools, check_sql


@pytest.mark.parametrize("sql", [
    "DELETE FROM breaks",
    "UPDATE breaks SET status = 'CLOSED'",
    "DROP TABLE trades",
    "INSERT INTO breaks (break_id) VALUES ('x')",
    "SELECT 1; DROP TABLE trades",
    "SELECT * FROM break_labels",
    "SELECT b.* FROM breaks b JOIN break_labels l USING (break_id)",
    "WITH x AS (SELECT * FROM break_labels) SELECT * FROM x",
    "SELECT load_extension('evil')",
    "ATTACH DATABASE 'x.db' AS x",
    "PRAGMA writable_schema = 1",
    "SELECT * FROM sqlite_master",
    "CREATE TABLE t AS SELECT * FROM trades",
])
def test_guard_blocks(sql):
    with pytest.raises(GuardError):
        check_sql(sql)


def test_guard_adds_and_caps_limit():
    assert "LIMIT 200" in check_sql("SELECT * FROM trades")
    assert "LIMIT 200" in check_sql("SELECT * FROM trades LIMIT 5000")
    assert "LIMIT 5" in check_sql("SELECT * FROM trades LIMIT 5")


def test_guard_allows_ctes_and_unions():
    out = check_sql("WITH t AS (SELECT trade_id FROM trades) SELECT * FROM t "
                    "UNION ALL SELECT trade_ref FROM custodian_settlements")
    assert "LIMIT" in out


def test_blocked_query_is_logged_not_raised(ro_engine):
    t = ReconTools(ro_engine)
    rec = t.run_sql("DELETE FROM breaks", "nope")
    assert rec.error and "blocked by guard" in rec.error
    assert t.queries[0].qid == "Q1"


def test_connection_is_read_only_even_without_guard(ro_engine):
    from sqlalchemy import text
    with ro_engine.connect() as c, pytest.raises(Exception, match="readonly"):
        c.execute(text("UPDATE breaks SET status = 'X'"))
