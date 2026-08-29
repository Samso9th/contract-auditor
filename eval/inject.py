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

# Each language brings its own fixture, its own mutations and its own build
# check. A language is only "supported" once it has all three and a passing
# evaluation - shipping a parser and calling that support would be claiming a
# contract we had not verified, which is the exact failure this tool exists to
# catch.
SUITES = {
    "go": {
        "fixture": ROOT / "fixture",
        "mutations": ROOT / "mutations" / "mutations.json",
        "cases": ROOT / "cases",
        "build": ["go", "build", "./..."],
    },
    "typescript": {
        "fixture": ROOT / "fixture-ts",
        "mutations": ROOT / "mutations" / "mutations-ts.json",
        "cases": ROOT / "cases-ts",
        # `node --check` on every source file: the analogue of `go build`, and
        # the same gate - a mutation that does not parse tests nothing.
        "build": None,
        "check": ["node", "--check"],
        "suffixes": (".js", ".mjs"),
    },
    "php": {
        "fixture": ROOT / "fixture-php",
        "mutations": ROOT / "mutations" / "mutations-php.json",
        "cases": ROOT / "cases-php",
        "build": None,
        "check": ["php", "-l"],
        "suffixes": (".php",),
    },
    "python": {
        "fixture": ROOT / "fixture-py",
        "mutations": ROOT / "mutations" / "mutations-py.json",
        "cases": ROOT / "cases-py",
        "build": None,
        "check": ["python3", "-m", "py_compile"],
        "suffixes": (".py",),
    },
}


def suite(language):
    if language not in SUITES:
        raise SystemExit(f"unknown language {language!r}; known: {', '.join(SUITES)}")
    return SUITES[language]


def load_mutations(language="go"):
    with open(suite(language)["mutations"]) as f:
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


def verify_build(case_dir, language="go"):
    """A mutation that does not build tests nothing. Gate on it."""
    spec = suite(language)
    api = case_dir / "api"

    if spec["build"]:
        result = subprocess.run(spec["build"], cwd=api, capture_output=True, text=True)
        return result.returncode == 0, result.stderr.strip()

    # Interpreted languages have no build step, so parse every source file.
    check = spec.get("check")
    if not check:
        return True, ""
    errors = []
    for suffix in spec.get("suffixes", ()):
        for path in sorted(api.rglob(f"*{suffix}")):
            result = subprocess.run(check + [str(path)], capture_output=True, text=True)
            if result.returncode != 0:
                errors.append(result.stderr.strip())
    return not errors, "\n".join(errors)


def build_case(mutation, quiet=False, language="go"):
    spec = suite(language)
    FIXTURE, CASES = spec["fixture"], spec["cases"]
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

    ok, stderr = verify_build(case_dir, language)
    if not ok:
        raise SystemExit(f"{mutation['id']}: mutated fixture does not build\n{stderr}")

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
    parser.add_argument("--language", default="go", choices=sorted(SUITES),
                        help="which language suite to build (default go)")
    args = parser.parse_args()

    mutations = load_mutations(args.language)
    CASES = suite(args.language)["cases"]

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
        build_case(m, language=args.language)
    drift = sum(1 for m in selected if m["expect"])
    print(f"\n{len(selected)} cases: {drift} with drift, {len(selected) - drift} decoys")


if __name__ == "__main__":
    main()
