"""Write the JSON the GitHub Pages demo replays.

    python -m breakbot.export_site

The site doesn't fake anything: every step, SQL statement, result row and
citation in docs/data/ comes from an actual run of the graph below.
"""
from __future__ import annotations

import json
import shutil

from sqlalchemy import create_engine, text

from .audit import Audit
from .benchmark import run
from .config import AS_OF, RECON_URL, ROOT
from .graph import build_graph
from .seed import ROOT_CAUSES
from .tools import GuardError, check_sql

GUARD_EXAMPLES = [
    "DELETE FROM breaks WHERE status = 'OPEN'",
    "SELECT * FROM break_labels",
    "SELECT 1; DROP TABLE trades",
    "UPDATE trades SET quantity = 1000 WHERE trade_id = 'T4181234'",
    "WITH k AS (SELECT * FROM break_labels) SELECT * FROM k",
    "SELECT pg_sleep(30)",
    "COPY trades TO '/tmp/trades.csv'",
    "SELECT * INTO trades_backup FROM trades",
    "SELECT set_config('default_transaction_read_only', 'off', false)",
    "SELECT * FROM pg_catalog.pg_roles",
]

OUT = ROOT / "docs" / "data"
ROWS_PER_QUERY = 14


def _dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, separators=(",", ":"), default=str))


def main() -> None:
    audit_path = ROOT / "data" / "audit_export.db"
    audit_path.unlink(missing_ok=True)
    audit = Audit(f"sqlite:///{audit_path}")
    graph = build_graph(audit=audit)
    results, metrics = run("playbook", graph=graph)

    eng = create_engine(RECON_URL)
    with eng.connect() as conn:
        secs = {r.security_id: dict(r._mapping) for r in conn.execute(text("SELECT * FROM securities"))}
        accts = [dict(r._mapping) for r in conn.execute(text("SELECT * FROM accounts ORDER BY account_id"))]
        tables = []
        for t in ("breaks", "ibor_positions", "custodian_positions", "trades", "custodian_settlements",
                  "corporate_actions", "ca_processing", "custodian_ca_postings", "prices", "fx_rates",
                  "ibor_cash_balances", "custodian_cash_balances", "ibor_cash_activity", "custodian_cash_activity",
                  "securities", "accounts", "break_labels"):
            cols = list(conn.execute(text(f"SELECT * FROM {t} LIMIT 0")).keys())
            n = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            tables.append(dict(name=t, columns=cols, rows=n, agent_access=t != "break_labels"))

    if (OUT / "runs").exists():
        shutil.rmtree(OUT / "runs")
    index = []
    for r in results:
        st = r["state"]
        b = st["break_row"]
        f = st["finding"]
        sec = secs.get(b.get("security_id")) if b.get("security_id") is not None else None
        index.append(dict(
            id=r["break_id"], account=b["account_id"], ticker=sec["ticker"] if sec else None,
            name=sec["name"] if sec else "Income cash", type=b["break_type"], ccy=b["currency"],
            sec_ccy=sec["currency"] if sec else b["currency"],
            ibor=b["ibor_value"], cust=b["custodian_value"], diff=b["difference"], usd=b["difference_usd"],
            truth=r["label"]["root_cause"], variant=r["label"]["variant"], pred=f["root_cause"],
            conf=f["confidence"], owner=f["owner_team"], q=len(st["queries"]), ms=round(r["ms"], 1),
            flags=st.get("needs_attention", []),
        ))
        trail = list(reversed(audit.trail(r["break_id"])))
        _dump(OUT / "runs" / f"{r['break_id']}.json", dict(
            break_row=b,
            security=sec,
            steps=st["steps"],
            queries=[dict(q, rows=q["rows"][:ROWS_PER_QUERY], truncated=q["row_count"] > ROWS_PER_QUERY)
                     for q in st["queries"]],
            finding=f,
            citation_check=st["citation_check"],
            needs_attention=st.get("needs_attention", []),
            label=r["label"],
            audit=[dict(ts=a["ts"], node=a["node"], event=a["event"], actor=a["actor"],
                        payload=json.loads(a["payload"]) if a["payload"] else None) for a in trail],
        ))

    _dump(OUT / "index.json", dict(as_of=AS_OF.isoformat(), causes=list(ROOT_CAUSES), accounts=accts,
                                   breaks=index, generated_at=metrics["generated_at"]))
    _dump(OUT / "benchmark.json", metrics)
    _dump(OUT / "schema.json", tables)
    refusals = []
    for sql in GUARD_EXAMPLES:
        try:
            check_sql(sql, "postgres")
            raise RuntimeError(f"guard let this through: {sql}")
        except GuardError as e:
            refusals.append(dict(sql=sql, error=str(e)))
    _dump(OUT / "guard_examples.json", refusals)
    audit_path.unlink(missing_ok=True)
    size = sum(p.stat().st_size for p in OUT.rglob("*.json"))
    print(f"wrote {len(index)} runs to {OUT.relative_to(ROOT)} ({size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
