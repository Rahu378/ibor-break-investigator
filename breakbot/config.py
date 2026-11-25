import datetime as dt
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("BREAKBOT_DATA_DIR", ROOT / "data"))

# recon warehouse (the agent only ever gets a read-only handle on this one)
RECON_URL = os.getenv("BREAKBOT_DB_URL", f"sqlite:///{DATA_DIR / 'recon.db'}")
# audit trail + review decisions live somewhere the agent can't touch
AUDIT_URL = os.getenv("BREAKBOT_AUDIT_URL", f"sqlite:///{DATA_DIR / 'audit.db'}")

MODEL = os.getenv("BREAKBOT_MODEL", "claude-opus-5-5")
EFFORT = os.getenv("BREAKBOT_EFFORT", "medium")

AS_OF = dt.date(2026, 11, 20)  # Friday. T+1 trades from today settle Monday.
MAX_ROWS = 200
STATEMENT_TIMEOUT_MS = 5000
