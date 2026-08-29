#!/usr/bin/env python3
"""Score a run against the injected ground truth.

Reads one findings file per case from a run directory and compares it to the
case's ground_truth.json. Works identically for the baseline and the agent, so
the comparison the hackathon asks for is a single flag apart.

    python3 score.py --run ../reports/runs/agent
    python3 score.py --run ../reports/runs/baseline --markdown

Findings file format - one JSON per case, named <CASE_ID>.json:

    {
      "case": "D01",
      "findings": [
        {"path": "/payouts", "method": "post",
         "kind": "response_field_mismatch", "detail": "fee",
         "severity": "high", "evidence": "handlers/types.go:24 json tag is feeAmount",
         "verified_by": "tests/contract_d01_test.go"}
      ],
      "meta": {"wall_clock_seconds": 41.8, "cost_usd": 0.08, "human_minutes": 0}
    }

Matching rule: a finding counts as a true positive when path, method and kind
all match an expected entry. `detail` is reported but not required to match, so
an auditor is not penalised for naming the offending field differently. Each
expected entry can be claimed once; extra findings are false positives. This
rule is deliberately generous on wording and strict on substance.
"""

import argparse
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_CASES = ROOT / "cases"


def norm(value):
    return str(value or "").strip().lower().rstrip("/") or "/"


def key(entry):
    return (norm(entry.get("path")), norm(entry.get("method")), norm(entry.get("kind")))


def score_case(case_dir, run_dir):
    with open(case_dir / "ground_truth.json") as f:
        truth = json.load(f)

    findings_path = run_dir / f"{truth['id']}.json"
    if not findings_path.exists():
        return {**truth, "missing": True, "tp": 0, "fp": 0,
                "fn": len(truth["expect"]), "found": [], "meta": {}}

    with open(findings_path) as f:
        payload = json.load(f)
    findings = payload.get("findings", [])

    unclaimed = [key(e) for e in truth["expect"]]
    tp, fp = 0, 0
    for finding in findings:
        k = key(finding)
        if k in unclaimed:
            unclaimed.remove(k)
            tp += 1
        else:
            fp += 1

    return {**truth, "missing": False, "tp": tp, "fp": fp, "fn": len(unclaimed),
            "found": findings, "meta": payload.get("meta", {})}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", required=True, help="directory of findings JSON files")
    parser.add_argument("--cases", default=str(DEFAULT_CASES),
                        help="case directory the run was scored against")
    parser.add_argument("--markdown", action="store_true", help="emit a markdown table")
    parser.add_argument("--json", action="store_true", help="emit raw JSON")
    args = parser.parse_args()

    run_dir = pathlib.Path(args.run).resolve()
    cases = pathlib.Path(args.cases).resolve()
    if not cases.exists():
        raise SystemExit(f"no cases at {cases}. Run: python3 inject.py --all")

    results = [score_case(d, run_dir) for d in sorted(cases.iterdir()) if d.is_dir()]

    tp = sum(r["tp"] for r in results)
    fp = sum(r["fp"] for r in results)
    fn = sum(r["fn"] for r in results)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    decoys = [r for r in results if not r["expect"]]
    clean_decoys = sum(1 for r in decoys if r["fp"] == 0)
    cost = sum(r["meta"].get("cost_usd", 0) for r in results)
    wall = sum(r["meta"].get("wall_clock_seconds", 0) for r in results)
    human = sum(r["meta"].get("human_minutes", 0) for r in results)

    summary = {
        "run": str(run_dir), "cases": len(results),
        "true_positives": tp, "false_positives": fp, "false_negatives": fn,
        "precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3),
        "decoys_clean": f"{clean_decoys}/{len(decoys)}",
        "total_cost_usd": round(cost, 4),
        "total_wall_clock_seconds": round(wall, 1),
        "human_minutes": human,
    }

    if args.json:
        print(json.dumps({"summary": summary, "cases": results}, indent=2))
        return

    if args.markdown:
        print(f"### Run: `{run_dir.name}`\n")
        print("| CASE | SEVERITY | CATEGORY | EXPECTED | TP | FP | FN | RESULT |")
        print("|---|---|---|---|---|---|---|---|")
        for r in results:
            expected = len(r["expect"])
            if r["missing"]:
                verdict = "no output"
            elif not expected:
                verdict = "clean" if r["fp"] == 0 else f"{r['fp']} false positive(s)"
            elif r["fn"] == 0 and r["fp"] == 0:
                verdict = "caught"
            elif r["fn"] == 0:
                verdict = "caught, with noise"
            else:
                verdict = "missed"
            print(f"| {r['id']} | {r['severity']} | {r['category']} | {expected} "
                  f"| {r['tp']} | {r['fp']} | {r['fn']} | {verdict} |")
        print()
        print("| METRIC | VALUE |")
        print("|---|---|")
        for k, v in summary.items():
            if k == "run":
                continue
            print(f"| {k.replace('_', ' ')} | {v} |")
        return

    print(f"run: {run_dir}")
    print(f"{'CASE':<5} {'SEV':<9} {'EXP':>3} {'TP':>3} {'FP':>3} {'FN':>3}  CATEGORY")
    for r in results:
        print(f"{r['id']:<5} {r['severity']:<9} {len(r['expect']):>3} {r['tp']:>3} "
              f"{r['fp']:>3} {r['fn']:>3}  {r['category']}")
    print()
    for k, v in summary.items():
        print(f"{k:<26} {v}")


if __name__ == "__main__":
    main()
