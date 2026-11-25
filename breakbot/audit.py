"""Append-only audit trail + analyst decisions.

Lives in its own database (AUDIT_URL) so the investigator's read-only
connection to the recon warehouse never has a reason to write anywhere.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from sqlalchemy import create_engine, text

from .config import AUDIT_URL

DDL = [
    """CREATE TABLE IF NOT EXISTS agent_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        break_id TEXT NOT NULL,
        ts TEXT NOT NULL,
        node TEXT NOT NULL,
        event TEXT NOT NULL,
        actor TEXT NOT NULL,
        payload TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS break_reviews (
        run_id TEXT PRIMARY KEY,
        break_id TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        reviewer TEXT NOT NULL,
        action TEXT NOT NULL,
        proposed_root_cause TEXT,
        final_root_cause TEXT,
        final_fix TEXT,
        final_owner TEXT,
        note TEXT
    )""",
]


class Audit:
    def __init__(self, url: str = AUDIT_URL):
        if url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url)
        pg = self.engine.dialect.name == "postgresql"
        with self.engine.begin() as conn:
            for ddl in DDL:
                if pg:
                    ddl = ddl.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
                conn.execute(text(ddl))

    def log(self, run_id: str, break_id: str, node: str, event: str, payload=None, actor: str = "agent") -> None:
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO agent_audit (run_id, break_id, ts, node, event, actor, payload) "
                "VALUES (:r, :b, :ts, :n, :e, :a, :p)"),
                dict(r=run_id, b=break_id, ts=dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
                     n=node, e=event, a=actor, p=json.dumps(payload, default=str) if payload is not None else None))

    def record_review(self, run_id: str, break_id: str, reviewer: str, action: str, proposed: str,
                      final_cause: str | None, final_fix: str | None, final_owner: str | None, note: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM break_reviews WHERE run_id = :r"), dict(r=run_id))
            conn.execute(text(
                "INSERT INTO break_reviews VALUES (:r, :b, :ts, :rv, :a, :p, :fc, :ff, :fo, :n)"),
                dict(r=run_id, b=break_id, ts=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                     rv=reviewer, a=action, p=proposed, fc=final_cause, ff=final_fix, fo=final_owner, n=note))

    def trail(self, break_id: str | None = None, limit: int = 500) -> list[dict]:
        q = "SELECT * FROM agent_audit"
        params = {}
        if break_id:
            q += " WHERE break_id = :b"
            params["b"] = break_id
        q += " ORDER BY id DESC LIMIT :l"
        params["l"] = limit
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(text(q), params)]

    def reviews(self) -> list[dict]:
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(text("SELECT * FROM break_reviews ORDER BY decided_at DESC"))]
