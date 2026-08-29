#!/usr/bin/env python3
"""Verification for the route extractor.

The extractor is the component every later finding rests on, so it is checked
against cases whose answers are known by construction rather than by inspection.
Standard library only; no test framework needed.

    python3 auditor/tools/test_routes.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from routes import extract, index_by_operation  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "eval" / "fixture"
CASES = ROOT / "eval" / "cases"

CLEAN_ROUTES = {
    ("/balance", "get"), ("/banks", "get"),
    ("/customers", "post"), ("/customers/{id}", "get"),
    ("/kyc/bvn", "post"), ("/payouts", "post"), ("/payouts/{id}", "get"),
    ("/refunds", "post"), ("/transactions", "get"), ("/webhooks/test", "post"),
}

results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))


def case_dir(case_id):
    return CASES / case_id / "api"


def main():
    if not CASES.exists():
        sys.exit("no cases built. Run: cd eval && python3 inject.py --all")

    # The clean fixture is the reference point. Ten routes, every one annotated,
    # no annotation left over.
    clean = extract(FIXTURE, strip_prefix="/v1")
    check("clean: 10 routes", clean["route_count"] == 10, f"got {clean['route_count']}")
    check("clean: route set exact",
          set(index_by_operation(clean)) == CLEAN_ROUTES,
          str(set(index_by_operation(clean)) ^ CLEAN_ROUTES))
    check("clean: every route annotated",
          clean["routes_without_annotation"] == 0,
          f"got {clean['routes_without_annotation']}")
    check("clean: no orphan annotations",
          len(clean["annotations_unrouted"]) == 0,
          f"got {len(clean['annotations_unrouted'])}")

    # No route registration call is spelled with a bare path and no handler, so
    # a handler-less route means the .Get() guard has regressed.
    check("clean: every route has a handler",
          all(r["handler"] for r in clean["routes"]),
          str([r["path"] for r in clean["routes"] if not r["handler"]]))

    # D03 adds POST /payouts/{id}/cancel in code only.
    d03 = extract(case_dir("D03"), strip_prefix="/v1")
    d03_index = index_by_operation(d03)
    check("D03: 11 routes", d03["route_count"] == 11, f"got {d03['route_count']}")
    check("D03: cancel route found", ("/payouts/{id}/cancel", "post") in d03_index)
    check("D03: cancel route has no annotation",
          d03_index.get(("/payouts/{id}/cancel", "post"), {}).get("annotation") is None)
    check("D03: exactly one unannotated route",
          d03["routes_without_annotation"] == 1,
          f"got {d03['routes_without_annotation']}")

    # D04 unregisters GET /banks and strips its annotation.
    d04 = extract(case_dir("D04"), strip_prefix="/v1")
    d04_index = index_by_operation(d04)
    check("D04: 9 routes", d04["route_count"] == 9, f"got {d04['route_count']}")
    check("D04: banks route gone", ("/banks", "get") not in d04_index)

    # Decoys must not perturb the route table at all - that is what makes them
    # decoys. Compare against the clean table exactly.
    for decoy in ("N01", "N02", "N03", "N04"):
        table = extract(case_dir(decoy), strip_prefix="/v1")
        check(f"{decoy}: route table unchanged",
              set(index_by_operation(table)) == CLEAN_ROUTES and table["route_count"] == 10,
              f"got {table['route_count']} routes")

    # Drifts that do not touch routing must also leave the table alone. If one
    # of these moved the count, the extractor is reacting to something it should
    # not see, and every downstream count would be wrong.
    for drift in ("D01", "D02", "D05", "D06", "D07", "D09", "D10", "D11", "D12"):
        table = extract(case_dir(drift), strip_prefix="/v1")
        check(f"{drift}: routing untouched",
              table["route_count"] == 10,
              f"got {table['route_count']}")

    # D08 adds an auth check inside the handler body without touching the
    # annotation. The extractor must still report the annotation as public - the
    # gap between the two is precisely the finding, and collapsing it here would
    # hide the drift.
    d08 = extract(case_dir("D08"), strip_prefix="/v1")
    banks = index_by_operation(d08)[("/banks", "get")]
    check("D08: routing untouched", d08["route_count"] == 10, f"got {d08['route_count']}")
    check("D08: banks annotation still public",
          not banks["annotation"]["security"],
          str(banks["annotation"]["security"]))

    # Path params normalise to OpenAPI braces so paths compare to the spec.
    check("path params normalised",
          all("{" in r["path"] for r in clean["routes"] if "customers/" in r["path"]))

    # Annotation parsing carries enough detail for downstream checks.
    payout = index_by_operation(clean)[("/payouts", "post")]["annotation"]
    check("annotation: statuses parsed",
          {s["code"] for s in payout["success"]} == {"201"}
          and {f["code"] for f in payout["failure"]} == {"400", "409"})
    check("annotation: idempotency header param parsed",
          any(p["name"] == "Idempotency-Key" and p["in"] == "header" and p["required"]
              for p in payout["params"]))
    check("annotation: typed schema detected",
          all(s["typed"] for s in payout["success"]))

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
