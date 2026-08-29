#!/usr/bin/env python3
"""Compare two scored runs: the self-improvement demonstration.

Run the same case set twice. The second run reads the first run's refutations as
retrieved negatives, so it should claim fewer things the gate has already
disproved. That is self-improvement with a number attached rather than a claim
on a slide, and it takes about two minutes of reading to check.

What to look at, in order of how much it means:

  * **claims refuted by the gate** - the direct measure. Retrieval is meant to
    stop a claim being made, so the count of claims that had to be executed and
    thrown away should fall.
  * **precision** - the reported consequence. It can only rise if a false
    positive was surviving the gate, so on a suite where the gate already
    catches everything it will sit still, and that is not a failure.
  * **recall** - the number that must not fall. If it does, memory is
    suppressing real drift, which is the failure mode this whole design is
    arranged to prevent, and the run is evidence against the mechanism.

    python3 eval/compare.py reports/runs/memory-1 reports/runs/memory-2
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from score import score_case  # noqa: E402


def summarise(run_dir, cases_dir):
    results = [score_case(d, run_dir) for d in sorted(cases_dir.iterdir()) if d.is_dir()]
    tp = sum(r["tp"] for r in results)
    fp = sum(r["fp"] for r in results)
    fn = sum(r["fn"] for r in results)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0

    gate = {"confirmed": 0, "refuted": 0, "unsupported": 0, "error": 0}
    recalled = explored = 0
    for r in results:
        for verdict, count in (r["meta"].get("verification") or {}).items():
            gate[verdict] = gate.get(verdict, 0) + count
        memory = r["meta"].get("memory") or {}
        recalled += memory.get("endpoints_with_recall", 0)
        explored += len(memory.get("explored_without_memory", []))

    return {
        "run": run_dir.name,
        "true positives": tp, "false positives": fp, "false negatives": fn,
        "precision": round(precision, 3), "recall": round(recall, 3),
        "f1": round(2 * precision * recall / (precision + recall), 3)
              if precision + recall else 0.0,
        "claims refuted by the gate": gate.get("refuted", 0),
        "claims confirmed by the gate": gate.get("confirmed", 0),
        "endpoints audited with recall": recalled,
        "endpoints explored without memory": explored,
        "cost usd": round(sum(r["meta"].get("cost_usd", 0) for r in results), 4),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("first")
    parser.add_argument("second")
    parser.add_argument("--cases", default=str(ROOT / "cases"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cases = pathlib.Path(args.cases).resolve()
    a = summarise(pathlib.Path(args.first).resolve(), cases)
    b = summarise(pathlib.Path(args.second).resolve(), cases)

    if args.json:
        print(json.dumps({"first": a, "second": b}, indent=2))
        return

    label = max(len(k) for k in a)
    print(f"{'':<{label}}  {a['run']:>14}  {b['run']:>14}  {'change':>8}")
    for k in a:
        if k == "run":
            continue
        before, after = a[k], b[k]
        delta = round(after - before, 3)
        arrow = "" if delta == 0 else f"{delta:+g}"
        print(f"{k:<{label}}  {before:>14}  {after:>14}  {arrow:>8}")

    print()
    if b["claims refuted by the gate"] < a["claims refuted by the gate"]:
        print("The second run wasted less work on claims the gate had already disproved.")
    elif b["claims refuted by the gate"] > a["claims refuted by the gate"]:
        print("The second run made more refuted claims than the first. Retrieval did not "
              "help here; the ledger is small, and one run of a stochastic model is not "
              "a trend.")
    else:
        print("The gate refuted the same number of claims in both runs.")
    if b["recall"] < a["recall"]:
        print("Recall fell. That is the failure this design exists to prevent: check "
              "which findings were lost before reading anything else as an improvement.")


if __name__ == "__main__":
    main()
