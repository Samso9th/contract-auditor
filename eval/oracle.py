#!/usr/bin/env python3
"""Emit a perfect run from the ground truth, to check the scorer itself.

Scoring this run must yield precision 1.0, recall 1.0 and 4/4 decoys clean. Any
other result means the scorer is broken rather than the agent - worth being able
to rule out without spending a token.

    python3 oracle.py [--out ../reports/runs/oracle]
"""

import argparse
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT.parent / "reports" / "runs" / "oracle"))
    args = parser.parse_args()

    cases = ROOT / "cases"
    if not cases.exists():
        raise SystemExit("no cases built. Run: python3 inject.py --all")

    out = pathlib.Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    written = 0
    for case_dir in sorted(cases.iterdir()):
        if not case_dir.is_dir():
            continue
        with open(case_dir / "ground_truth.json") as f:
            truth = json.load(f)
        with open(out / f"{truth['id']}.json", "w") as f:
            json.dump({"case": truth["id"], "findings": truth["expect"], "meta": {}}, f, indent=2)
            f.write("\n")
        written += 1

    print(f"wrote {written} oracle findings to {out}")


if __name__ == "__main__":
    main()
