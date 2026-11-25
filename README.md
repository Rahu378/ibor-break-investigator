# ibor-break-investigator

An agent that works IBOR vs custodian breaks the way a recon analyst does. It pulls both positions, the recent trades and their settlement status, corporate actions, and the prices and FX each book used. Then it names the root cause and cites the rows it relied on. A person approves, edits or rejects before anything closes.

**Live demo: [rahu378.github.io/ibor-break-investigator](https://rahu378.github.io/ibor-break-investigator/)**

The demo replays recorded runs from this repo. Every query, row and citation on that page came out of `python -m breakbot.export_site`.

![Break Investigator](docs/assets/preview.png)

## Where it landed

| | |
|---|---|
| Breaks in the benchmark | 200 (12 funds, 41 securities, 7 currencies) |
| Matched the seeded root cause | **194 / 200 (97.0%)** |
| Wrong calls | **0**. The other 6 came back "unexplained" and went to a human |
| Findings whose citations point at real rows | 200 / 200 |
| Read-only queries per break | 4.4 |
| Runtime per break (playbook, SQLite) | p50 29 ms, p95 37 ms |

I get the same 194/200 on Postgres 16 running as the read-only role.

Those are the deterministic investigator's numbers. The Claude investigator plugs into the same tools, graph and benchmark:

```bash
ANTHROPIC_API_KEY=... python -m breakbot.benchmark --mode llm
```

The six it abstains on are cases a fixed playbook can't see (more below). That's where letting the model write its own SQL should pay off.

## Root causes

| Cause | What it looks like in the data | Goes to |
|---|---|---|
| Timing | IBOR books on trade date, the custodian on settlement. A T+1 trade from Friday settles Monday | nobody, auto-recheck |
| Missed corporate action | split or stock dividend IBOR never processed (or processed at the wrong ratio), dividends the custodian credited and IBOR didn't book | Corporate Actions |
| Stale price | golden copy stopped getting a feed, or stamped yesterday's close with today's date | Pricing & Valuations |
| FX | valuation batch picked up T-1 WMR or the 10am intraday fixing instead of WMR 4pm | Market Data - FX |
| Failed settlement | past contractual settlement and failed, partial, or still "pending" | Settlements |
| Booking error | fat finger, transposed digits, wrong side, wrong account, wrong security | Trade Support |

## How it works

```
load_break -> investigate -> check_citations -> propose_fix -> human_review -> close
                                                                    `-> reopen (on reject)
```

- **LangGraph** state machine (`breakbot/graph.py`). `human_review` calls `interrupt()`, so a run parks with its state in the checkpointer until an analyst resumes it with a decision.
- **Tools** (`breakbot/tools.py`) are read-only SQL. Every statement goes through a sqlglot guard first: one SELECT, allow-listed tables, no `pg_sleep` / `set_config` / `load_extension`, no `SELECT INTO`, LIMIT added or capped. Then the connection itself is read-only (SQLite `mode=ro`, on Postgres a SELECT-only role with `default_transaction_read_only`). I wanted two independent locks, so there's a test that the database refuses writes even when the guard is skipped.
- Every call gets a query id. Findings have to cite `[Q3:r1]` style references, and `check_citations` flags anything that points at a query or row that doesn't exist.
- **Two investigators.** `playbook.py` walks the tools in a fixed order and scores each hypothesis by how much of the break it explains. `llm.py` gives Claude the same tools plus free-form `run_sql`, and it has to finish with a `submit_finding` tool call.
- **Audit** (`audit.py`): every node, query and decision is appended to a separate store. The agent's database handle can't write to it.
- **Streamlit** (`app.py`) is the review queue.

## The data

`breakbot/seed.py` builds two books from one trade history (IBOR on trade date, custodian on settlement), injects faults, then runs a plain recon over them (outer join + tolerance). The breaks are whatever falls out of that, so the labels are honest. The answer key (`break_labels`) sits in the same database and the agent still can't read it: it isn't on the allow-list and it isn't granted to the role.

Things I made sure were in there, because real breaks have them:

- one line with two causes (a stale price on a position that also got the wrong FX fixing). The label is whichever moved the number more.
- a trade booked to the wrong account, which shows up as two breaks, one per account.
- fails still sitting at "PENDING" days after settlement date, and partial deliveries.
- dividends IBOR booked gross while the custodian paid net of withholding.
- an aged fail from three weeks back, outside the default lookback window.
- a trade booked against the wrong security.

The last three are what the playbook abstains on. That's deliberate. A benchmark a hard-coded playbook scores 100% on wouldn't tell me anything.

Securities use real tickers and ISINs (the generator checks every ISIN's check digit). Prices, positions, funds and counterparties are made up.

## Run it

```bash
pip install -r requirements.txt
python -m breakbot.seed          # sqlite at data/recon.db
python -m breakbot.benchmark     # writes reports/benchmark_playbook.json
streamlit run app.py
python -m pytest -q
```

On Postgres:

```bash
docker compose up -d
python -m breakbot.seed --url postgresql+psycopg://recon_admin:recon_admin@localhost:5432/recon
psql postgresql://recon_admin:recon_admin@localhost:5432/recon -v agent_password=change-me -f sql/readonly_role.sql
export BREAKBOT_DB_URL=postgresql+psycopg://breakbot_agent:change-me@localhost:5432/recon
```

Refresh the demo data with `python -m breakbot.export_site`.

## Layout

```
breakbot/
  seed.py         synthetic books, recon, labels
  tools.py        sqlglot guard, read-only engine, query log
  playbook.py     deterministic investigator
  llm.py          Claude investigator (tool use)
  graph.py        LangGraph workflow with the review interrupt
  audit.py        audit trail and review decisions
  benchmark.py    scoring against the labels
  export_site.py  writes docs/data for the Pages demo
app.py            Streamlit review queue
sql/              Postgres schema and read-only role
docs/             GitHub Pages site
tests/            guard, seed, playbook, graph, and the Claude loop against a fake client
```

## Notes and next steps

- The checkpointer is in-memory for now, so a parked review doesn't survive a restart. The Postgres checkpointer is the obvious next step.
- No holiday calendar yet. Thanksgiving week would throw off the T+1 math.
- I want to run the Claude benchmark at a couple of effort levels and compare cost per correctly closed break against the playbook.
- Cash recon only covers income cash. Trade cash legs would be the next layer.
