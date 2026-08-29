#!/usr/bin/env python3
"""Verification for the deterministic rule layer.

    python3 auditor/tools/test_diff.py
"""

import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diff import audit  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "eval" / "fixture"
CASES = ROOT / "eval" / "cases"

# The kinds each case must produce. Cases absent from this table are expected to
# produce nothing from the deterministic layer - they need judgment, and claiming
# them here without a rule would be scoring against a rule that does not exist.
EXPECTED_KINDS = {
    "D01": {"response_field_mismatch"},
    "D02": {"status_code_mismatch"},
    "D03": {"route_missing_from_spec"},
    "D04": {"route_missing_from_code"},
    "D05": {"response_type_mismatch"},
    "D07": {"request_param_mismatch"},
    "D08": {"auth_mismatch", "undocumented_status"},
    "D10": {"response_header_mismatch"},
    "D12": {"undocumented_status"},
}
SILENT = {"D06", "D09", "D11", "N01", "N02", "N03", "N04"}

results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))


def run(directory):
    return audit(directory / "api" if (directory / "api").exists() else directory,
                 directory / "spec" / "openapi.json" if (directory / "spec").exists()
                 else directory / "spec" / "openapi.json",
                 strip_prefix="/v1")


def main():
    if not CASES.exists():
        sys.exit("no cases built. Run: cd eval && python3 inject.py --all")

    clean = audit(FIXTURE, FIXTURE / "spec" / "openapi.json", strip_prefix="/v1")
    check("clean fixture produces no findings", clean == [], f"got {len(clean)}")

    # Determinism. Go randomises map iteration, and an earlier version of the
    # extractor leaked that randomness into the report: five identical runs over
    # the same source gave five different answers. A report that changes when
    # nothing changed is not evidence.
    digests = set()
    for _ in range(3):
        findings = audit(CASES / "D08" / "api", CASES / "D08" / "spec" / "openapi.json",
                         strip_prefix="/v1")
        digests.add(hashlib.sha256(json.dumps(findings, sort_keys=True).encode()).hexdigest())
    check("repeated runs are byte-identical", len(digests) == 1, f"{len(digests)} distinct results")

    for case_id, kinds in sorted(EXPECTED_KINDS.items()):
        case = CASES / case_id
        findings = audit(case / "api", case / "spec" / "openapi.json", strip_prefix="/v1")
        got = {f["kind"] for f in findings}
        check(f"{case_id}: produces {'+'.join(sorted(kinds))}", got == kinds, f"got {sorted(got)}")
        check(f"{case_id}: every finding cites a location",
              all(":" in f["evidence"] or "spec documents" in f["evidence"] for f in findings))

    for case_id in sorted(SILENT):
        case = CASES / case_id
        findings = audit(case / "api", case / "spec" / "openapi.json", strip_prefix="/v1")
        label = "decoy stays clean" if case_id.startswith("N") else "correctly declines"
        check(f"{case_id}: {label}", findings == [], f"got {[f['kind'] for f in findings]}")

    width = max(len(n) for n, _, _ in results)
    failed = 0
    for name, ok, detail in results:
        status = "pass" if ok else "FAIL"
        line = f"  {status}  {name:<{width}}"
        if not ok and detail:
            line += f"   {detail}"
        print(line)
        failed += not ok

    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
