#!/usr/bin/env python3
"""Verification for the verification gate.

The gate is only worth having if it discriminates. Confirming real drift is the
easy half; the half that matters is refusing false claims, so most of this file
asserts that claims which are *not* true get refuted rather than waved through.

    python3 auditor/test_verify.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from verify import verify_claim  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "eval" / "fixture"
CASES = ROOT / "eval" / "cases"

results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))


def claim(path, method, kind, detail=""):
    return {"path": path, "method": method, "kind": kind, "detail": detail}


# Claims that are true of the mutated code. Each must be confirmed by execution.
TRUE_CLAIMS = [
    ("D01", claim("/payouts", "post", "response_field_mismatch", "fee")),
    ("D01", claim("/payouts/{id}", "get", "response_field_mismatch", "fee")),
    ("D02", claim("/customers", "post", "status_code_mismatch", "201")),
    ("D05", claim("/balance", "get", "response_type_mismatch", "available")),
    ("D05", claim("/balance", "get", "response_type_mismatch", "ledger")),
    ("D08", claim("/banks", "get", "auth_mismatch", "ApiKeyAuth")),
    ("D10", claim("/webhooks/test", "post", "response_header_mismatch", "X-Signature")),
]

# The same claims against the clean fixture, where none of them are true. This
# is the test that decides whether the gate is worth anything.
FALSE_CLAIMS = [
    claim("/payouts", "post", "response_field_mismatch", "fee"),
    claim("/payouts/{id}", "get", "response_field_mismatch", "fee"),
    claim("/customers", "post", "status_code_mismatch", "201"),
    claim("/balance", "get", "response_type_mismatch", "available"),
    claim("/banks", "get", "auth_mismatch", "ApiKeyAuth"),
    claim("/webhooks/test", "post", "response_header_mismatch", "X-Signature"),
]

# Claims naming things that do not exist. These must never be confirmed.
FABRICATED = [
    claim("/payouts", "post", "response_field_mismatch", "discountRate"),
    claim("/balance", "get", "response_type_mismatch", "cryptoBalance"),
]


def main():
    if not CASES.exists():
        sys.exit("no cases built. Run: cd eval && python3 inject.py --all")

    for case_id, c in TRUE_CLAIMS:
        outcome = verify_claim(CASES / case_id, c)
        check(f"{case_id}: confirms true {c['kind']} ({c['detail']})",
              outcome["verdict"] == "confirmed",
              f"{outcome['verdict']}: {outcome['detail']}")
        check(f"{case_id}: evidence cites observed behaviour",
              outcome["verdict"] != "confirmed" or len(outcome["detail"]) > 20)

    for c in FALSE_CLAIMS:
        outcome = verify_claim(FIXTURE, c)
        check(f"clean: refutes false {c['kind']} on {c['path']}",
              outcome["verdict"] == "refuted",
              f"{outcome['verdict']}: {outcome['detail']}")

    for c in FABRICATED:
        outcome = verify_claim(FIXTURE, c)
        check(f"clean: rejects fabricated field {c['detail']!r}",
              outcome["verdict"] != "confirmed",
              f"{outcome['verdict']}: {outcome['detail']}")
        outcome = verify_claim(CASES / "D01", c)
        check(f"D01: rejects fabricated field {c['detail']!r}",
              outcome["verdict"] != "confirmed",
              f"{outcome['verdict']}: {outcome['detail']}")

    # A decoy changes no contract, so every claim against one must be refuted.
    for decoy in ("N01", "N03", "N04"):
        outcome = verify_claim(CASES / decoy,
                               claim("/payouts", "post", "response_field_mismatch", "fee"))
        check(f"{decoy}: refutes claim against a decoy",
              outcome["verdict"] == "refuted",
              f"{outcome['verdict']}: {outcome['detail']}")

    # Kinds the gate cannot execute must be declared unsupported, never assumed.
    outcome = verify_claim(CASES / "D09",
                           claim("/transactions", "get", "default_value_mismatch", "perPage"))
    check("declares unexecutable kinds unsupported", outcome["verdict"] == "unsupported",
          outcome["verdict"])

    # No generated test may be left behind in the case source.
    leftover = list(CASES.rglob("contract_verify_test.go"))
    check("generated tests are cleaned up", not leftover, str(leftover[:3]))

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
