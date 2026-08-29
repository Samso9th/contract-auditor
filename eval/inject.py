#!/usr/bin/env python3
"""Build evaluation cases by injecting known contract drift into the fixture API.

Each case is a self-contained directory holding a mutated copy of the fixture
source, the (unchanged) published spec, and the ground truth describing exactly
what a correct auditor should report. Standard library only.

    python3 inject.py --all              # build every case
    python3 inject.py --case D05         # build one case
    python3 inject.py --list             # show the case table
"""

import argparse
import json
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
FIXTURE = ROOT / "fixture"
CASES = ROOT / "cases"
MUTATIONS = ROOT / "mutations" / "mutations.json"


def load_mutations():
    with open(MUTATIONS) as f:
        return json.load(f)["mutations"]


def apply_edits(case_dir, mutation):
    """Apply every edit literally. A find string that is absent or ambiguous is
    a broken mutation, not a soft failure - raise so it is fixed at authoring
    time rather than silently skewing the score."""
    for edit in mutation["edits"]:
        target = case_dir / "api" / edit["file"]
        source = target.read_text()
        hits = source.count(edit["find"])
        if hits != 1:
            raise SystemExit(
                f"{mutation['id']}: edit in {edit['file']} matched {hits} times, expected exactly 1"
            )
        target.write_text(source.replace(edit["find"], edit["replace"]))


def verify_build(case_dir):
    """A mutation that does not compile tests nothing. Gate on it."""
    result = subprocess.run(
        ["go", "build", "./..."],
        cwd=case_dir / "api",
        capture_output=True,
        text=True,
    )
    return result.returncode == 0, result.stderr.strip()


def build_case(mutation, quiet=False):
    case_dir = CASES / mutation["id"]
    if case_dir.exists():
        shutil.rmtree(case_dir)
    (case_dir / "api").mkdir(parents=True)

    for item in FIXTURE.iterdir():
        if item.name in {"spec", "case.json"}:
            continue
        dest = case_dir / "api" / item.name
        shutil.copytree(item, dest) if item.is_dir() else shutil.copy2(item, dest)

    shutil.copytree(FIXTURE / "spec", case_dir / "spec")
    apply_edits(case_dir, mutation)

    ok, stderr = verify_build(case_dir)
    if not ok:
        raise SystemExit(f"{mutation['id']}: mutated fixture does not compile\n{stderr}")

    with open(case_dir / "ground_truth.json", "w") as f:
        json.dump(
            {
                "id": mutation["id"],
                "category": mutation["category"],
                "severity": mutation["severity"],
                "description": mutation["description"],
                "expect": mutation["expect"],
            },
            f,
            indent=2,
        )
        f.write("\n")

    if not quiet:
        kind = "decoy" if not mutation["expect"] else mutation["expect"][0]["kind"]
        print(f"  {mutation['id']}  {kind:<28} build ok")
    return case_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="build every case")
    parser.add_argument("--case", help="build a single case by id")
    parser.add_argument("--list", action="store_true", help="list cases")
    args = parser.parse_args()

    mutations = load_mutations()

    if args.list:
        print(f"{'ID':<5} {'SEVERITY':<10} {'CATEGORY':<28} DESCRIPTION")
        for m in mutations:
            print(f"{m['id']:<5} {m['severity']:<10} {m['category']:<28} {m['description'][:70]}")
        return

    if args.case:
        selected = [m for m in mutations if m["id"] == args.case]
        if not selected:
            sys.exit(f"unknown case: {args.case}")
    elif args.all:
        selected = mutations
    else:
        parser.print_help()
        return

    CASES.mkdir(exist_ok=True)
    print(f"building {len(selected)} case(s) into {CASES.relative_to(ROOT.parent)}/")
    for m in selected:
        build_case(m)
    drift = sum(1 for m in selected if m["expect"])
    print(f"\n{len(selected)} cases: {drift} with drift, {len(selected) - drift} decoys")


if __name__ == "__main__":
    main()
