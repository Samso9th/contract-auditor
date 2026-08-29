#!/usr/bin/env python3
"""Run the deterministic layer over every evaluation case.

No model, no API key, no cost. This is the number worth having before any agent
is built: it says how much of the problem does not need one, and it is the bar
the agent layer has to clear to justify its existence.

    python3 auditor/run_deterministic.py --cases eval/cases --out reports/runs/deterministic
"""

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "tools"))
from diff import audit  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", default="eval/cases")
    parser.add_argument("--out", default="reports/runs/deterministic")
    parser.add_argument("--strip-prefix", default="/v1")
    parser.add_argument("--language", default=None,
                        help="go or typescript; detected when omitted")
    args = parser.parse_args()

    cases = pathlib.Path(args.cases).resolve()
    if not cases.is_dir():
        sys.exit(f"no cases at {cases}. Run: cd eval && python3 inject.py --all")

    out = pathlib.Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    total = 0
    for case_dir in sorted(cases.iterdir()):
        if not case_dir.is_dir():
            continue
        started = time.time()
        findings = audit(case_dir / "api", case_dir / "spec" / "openapi.json",
                         strip_prefix=args.strip_prefix, language=args.language)
        elapsed = time.time() - started

        with open(out / f"{case_dir.name}.json", "w") as f:
            json.dump({
                "case": case_dir.name,
                "findings": findings,
                "meta": {
                    "wall_clock_seconds": round(elapsed, 2),
                    "cost_usd": 0.0,
                    "human_minutes": 0,
                    "layer": "deterministic",
                },
            }, f, indent=2)
            f.write("\n")

        kinds = ", ".join(sorted({f["kind"] for f in findings})) or "none"
        print(f"  {case_dir.name}  {len(findings)} finding(s)  {kinds}")
        total += len(findings)

    print(f"\n{total} findings written to {out}")


if __name__ == "__main__":
    main()
