"""Score the investigator against the seeded labels.

    python -m breakbot.benchmark                 # deterministic playbook
    python -m breakbot.benchmark --mode llm      # Claude (needs ANTHROPIC_API_KEY)
    python -m breakbot.benchmark --mode llm --limit 40

Each break runs through the real graph up to the human-review interrupt, so the
citation check and audit logging are part of what's measured.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import create_engine, text

from .config import RECON_URL, ROOT
from .graph import build_graph, start
from .playbook import CAUSES

REPORTS = ROOT / "reports"


def load_labels(url: str = RECON_URL) -> dict[str, dict]:
    # deliberately a separate, normal connection: the agent's handle can't see this table
    eng = create_engine(url)
    with eng.connect() as conn:
        rows = conn.execute(text("SELECT break_id, root_cause, variant FROM break_labels ORDER BY break_id"))
        return {r.break_id: dict(root_cause=r.root_cause, variant=r.variant) for r in rows}


def run(mode: str = "playbook", limit: int | None = None, graph=None, progress: bool = True):
    labels = load_labels()
    ids = list(labels)[:limit] if limit else list(labels)
    graph = graph or build_graph()
    results = []
    for n, bid in enumerate(ids, 1):
        t0 = time.perf_counter()
        cfg, st = start(graph, bid, mode=mode)
        ms = (time.perf_counter() - t0) * 1000
        results.append(dict(break_id=bid, label=labels[bid], state=st, ms=ms, thread=cfg["configurable"]["thread_id"]))
        if progress and (n % 25 == 0 or n == len(ids)):
            print(f"  {n}/{len(ids)}")
    return results, score(results, mode)


def score(results: list[dict], mode: str) -> dict:
    conf = defaultdict(Counter)
    variants = defaultdict(lambda: [0, 0])
    correct = abstained = cited = 0
    nq, lat = [], []
    for r in results:
        truth = r["label"]["root_cause"]
        f = r["state"]["finding"]
        pred = f["root_cause"]
        conf[truth][pred] += 1
        key = (truth, r["label"]["variant"])
        variants[key][0] += 1
        if pred == truth:
            correct += 1
            variants[key][1] += 1
        elif pred == "UNEXPLAINED":
            abstained += 1
        cited += bool(r["state"]["citation_check"]["ok"])
        nq.append(len(r["state"]["queries"]))
        lat.append(r["ms"])
    n = len(results)
    per = {}
    for c in CAUSES:
        tp = conf[c][c]
        support = sum(conf[c].values())
        predicted = sum(conf[t][c] for t in conf)
        per[c] = dict(support=support, tp=tp, precision=round(tp / predicted, 4) if predicted else None,
                      recall=round(tp / support, 4) if support else None)
    lat_sorted = sorted(lat)
    return dict(
        mode=mode,
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        n=n,
        correct=correct,
        accuracy=round(correct / n, 4),
        abstained=abstained,
        misclassified=n - correct - abstained,
        accuracy_when_answered=round(correct / (n - abstained), 4) if n - abstained else None,
        citation_ok=cited,
        citation_rate=round(cited / n, 4),
        avg_queries=round(statistics.mean(nq), 2),
        p50_ms=round(statistics.median(lat), 1),
        p95_ms=round(lat_sorted[int(0.95 * (n - 1))], 1),
        per_class=per,
        confusion={t: dict(conf[t]) for t in CAUSES},
        by_variant=[dict(cause=k[0], variant=k[1], n=v[0], correct=v[1]) for k, v in sorted(variants.items())],
        misses=[dict(break_id=r["break_id"], truth=r["label"]["root_cause"], variant=r["label"]["variant"],
                     predicted=r["state"]["finding"]["root_cause"]) for r in results
                if r["state"]["finding"]["root_cause"] != r["label"]["root_cause"]],
    )


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["playbook", "llm"], default="playbook")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args(argv)
    if args.mode == "llm":
        from . import llm
        if not llm.available():
            raise SystemExit("set ANTHROPIC_API_KEY to run the llm benchmark")
    print(f"benchmark mode={args.mode}")
    _, m = run(args.mode, args.limit)
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"benchmark_{args.mode}.json"
    out.write_text(json.dumps(m, indent=2))
    print(f"\nmatched labels   {m['correct']}/{m['n']}  ({m['accuracy']:.1%})")
    print(f"abstained        {m['abstained']}   misclassified {m['misclassified']}")
    print(f"cited evidence   {m['citation_ok']}/{m['n']}  ({m['citation_rate']:.0%})")
    print(f"queries / break  {m['avg_queries']}   p50 {m['p50_ms']} ms   p95 {m['p95_ms']} ms")
    print("\nper class:")
    for c, v in m["per_class"].items():
        print(f"  {c:<18} recall {v['recall']:.0%}  precision {v['precision'] if v['precision'] is None else format(v['precision'], '.0%')}"
              f"  (n={v['support']})")
    if m["misses"]:
        print("\nmisses:")
        for x in m["misses"]:
            print(f"  {x['break_id']}  {x['truth']:<18} {x['variant']:<32} -> {x['predicted']}")
    print(f"\nwrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
