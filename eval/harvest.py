#!/usr/bin/env python3
"""Grow the evaluation set from the field: mechanism 5 of the self-improvement design.

This is the mechanism most commonly skipped, and the one that decides whether
the rest of it means anything. Nearly every self-improving agent improves the
policy while the benchmark stays frozen, at which point a rising score is
indistinguishable from overfitting to a fixed test. Growing the test set
alongside the agent is what makes the trend line evidence.

Two sources, both already labelled by the gate and the scorer:

  * a **confirmed** drift found in a real repository becomes a new mutation -
    a fault the suite did not previously contain;
  * a **false positive that survived the gate** becomes a new decoy - the
    hardest kind of negative there is, because it already defeated the one
    mechanism built to catch it.

Everything harvested is tagged `"source": "field"`, so it stays separable from
the twelve hand-written cases and a score can always be reported both ways. A
deterministic third is marked `holdout: true` and must be kept out of any set
used for prompt iteration, for the same reason the benchmark is grown at all.

    python3 eval/harvest.py --run ../reports/runs/agent
    python3 eval/harvest.py --list
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import zlib

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "auditor" / "memory"))

import ledger as ledger_mod  # noqa: E402
import store as store_mod  # noqa: E402

FIELD = ROOT / "mutations" / "field.json"
DEFAULT_CASES = ROOT / "cases"

# A third held out. Deterministic on the case key, so the same case is held out
# on every machine and the split cannot be quietly reshuffled until it flatters
# a result.
HOLDOUT_SHARE = 3


def case_key(row):
    return f'{row.get("repo", "")}|{row.get("endpoint", "")}|{row.get("kind", "")}'


def holdout(key):
    return zlib.crc32(key.encode()) % HOLDOUT_SHARE == 0


def load(path=FIELD):
    path = pathlib.Path(path)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text()).get("mutations", [])
    except json.JSONDecodeError:
        return []


def save(entries, path=FIELD):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "_comment": "Cases harvested from real audits, not hand-written. A confirmed "
                    "drift becomes a mutation; a false positive that survived the "
                    "verification gate becomes a decoy. Entries carry source:field so "
                    "they stay separable from the twelve authored cases, and "
                    "holdout:true marks the third that must not be used for prompt "
                    "iteration. `edits` is empty until someone ports the drift into "
                    "the fixture - a case with no edits is a lead, not a test, and "
                    "inject.py will refuse to build it. Regenerate with "
                    "`python3 eval/harvest.py --run <run dir>`.",
        "mutations": entries,
    }, indent=2) + "\n")
    return path


def truth_keys(cases_dir):
    """What the authored cases already expect, so a harvested confirmation of a
    known fault is not filed as a discovery."""
    keys = set()
    cases_dir = pathlib.Path(cases_dir)
    if not cases_dir.is_dir():
        return keys
    for case in sorted(cases_dir.iterdir()):
        truth = case / "ground_truth.json"
        if not truth.exists():
            continue
        for expect in json.loads(truth.read_text()).get("expect", []):
            keys.add((str(expect.get("path")), str(expect.get("method")).lower(),
                      str(expect.get("kind"))))
    return keys


def survived_false_positives(run_dir, cases_dir):
    """Findings the gate kept that the ground truth says are wrong.

    Only knowable where ground truth exists, which is the evaluation cases. In a
    real repository the equivalent signal is a human dismissing a finding, which
    arrives through the allowlist rather than through here.
    """
    run_dir, cases_dir = pathlib.Path(run_dir), pathlib.Path(cases_dir)
    out = []
    for case in sorted(cases_dir.iterdir()) if cases_dir.is_dir() else []:
        truth_path, report_path = case / "ground_truth.json", run_dir / f"{case.name}.json"
        if not truth_path.exists() or not report_path.exists():
            continue
        expected = {(str(e.get("path")), str(e.get("method")).lower(), str(e.get("kind")))
                    for e in json.loads(truth_path.read_text()).get("expect", [])}
        for finding in json.loads(report_path.read_text()).get("findings", []):
            key = (str(finding.get("path")), str(finding.get("method")).lower(),
                   str(finding.get("kind")))
            if key not in expected:
                out.append(dict(finding, case=case.name))
    return out


def harvest(rows, false_positives, known, existing):
    """Merge new leads into the field set, keeping ids stable."""
    entries = {e["key"]: e for e in existing}
    counter = len(entries)

    def add(key, payload):
        nonlocal counter
        if key in entries:
            entries[key]["seen"] = entries[key].get("seen", 1) + 1
            return
        counter += 1
        entries[key] = dict(payload, id=f"F{counter:02d}", key=key, seen=1,
                            source="field", holdout=holdout(key), edits=[])

    for row in rows:
        if row.get("verdict") != "confirmed":
            continue
        method, _, path = str(row.get("endpoint", "")).partition(" ")
        if (path, method.lower(), row.get("kind")) in known:
            continue  # the suite already tests this fault
        add(case_key(row), {
            "category": f"field-{row.get('kind', 'drift')}".replace("_", "-"),
            "severity": "high" if row.get("kind") in
                        ("request_required_mismatch", "response_field_mismatch") else "medium",
            "description": f"Confirmed in {row.get('repo') or 'a real repository'} on "
                           f"{row.get('endpoint')}: {row.get('claim') or row.get('kind')}. "
                           f"Gate: {(row.get('evidence') or {}).get('gate', '')[:160]}",
            "expect": [{"path": path, "method": method.lower(),
                        "kind": row.get("kind"), "detail": row.get("claim", "")}],
        })

    for finding in false_positives:
        add(f'fp|{finding.get("case")}|{finding.get("path")}|{finding.get("kind")}', {
            "category": f"field-decoy-{finding.get('kind', '')}".replace("_", "-"),
            "severity": "info",
            "description": f"Decoy. This claim survived the verification gate on case "
                           f"{finding.get('case')} and was still wrong: "
                           f"{str(finding.get('evidence', ''))[:160]}",
            # An empty expect is what makes it a decoy: any finding here is a
            # false positive, which is precisely what this case exists to measure.
            "expect": [],
        })

    return sorted(entries.values(), key=lambda e: e["id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", help="run directory to harvest false positives from")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--memory-url", default="",
                        help="overrides AUDITOR_MEMORY_URL; file:///path for local")
    parser.add_argument("--field", default=str(FIELD))
    parser.add_argument("--list", action="store_true", help="show the field set and stop")
    args = parser.parse_args()

    existing = load(args.field)
    if args.list:
        if not existing:
            print(f"no field cases yet ({args.field})")
            return
        for entry in existing:
            mark = "holdout" if entry.get("holdout") else "train  "
            ready = "ready" if entry.get("edits") else "needs edits"
            print(f"  {entry['id']}  {mark}  {ready:<12} {entry['category']:<38} "
                  f"{entry['description'][:80]}")
        return

    store = store_mod.open_store(args.memory_url)
    rows = []
    for line in store.load():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    false_positives = survived_false_positives(args.run, args.cases) if args.run else []
    entries = harvest(rows, false_positives, truth_keys(args.cases), existing)
    save(entries, args.field)

    new = len(entries) - len(existing)
    decoys = sum(1 for e in entries if not e["expect"])
    held = sum(1 for e in entries if e.get("holdout"))
    print(f"{len(entries)} field case(s) (+{new} new): "
          f"{len(entries) - decoys} mutation(s), {decoys} decoy(s), {held} held out")
    print(f"written to {args.field}")
    if any(not e["edits"] for e in entries):
        print("\nCases with no `edits` are leads, not tests: port the drift into the "
              "fixture before inject.py will build them.")


if __name__ == "__main__":
    main()
