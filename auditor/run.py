#!/usr/bin/env python3
"""The contract auditor — deterministic rules, agent, verification gate.

Three layers, in the order that keeps the cheap and certain work ahead of the
expensive and uncertain:

  1. Deterministic rules parse code and spec and settle everything mechanical.
  2. A per-endpoint agent reads what the rules cannot settle, told what they
     already found so it does not restate it.
  3. Every claim from either layer is executed by the verification gate. A claim
     whose test passes is dropped, whatever produced it.

    python3 auditor/run.py --cases eval/cases --out reports/runs/agent
    python3 auditor/run.py --repo <dir> --spec <openapi.json> --out reports/runs/<name>
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "tools"))

from audit_endpoint import audit_endpoint  # noqa: E402
from diff import audit as deterministic_audit  # noqa: E402
from llm import DEFAULT_MODEL  # noqa: E402
from routes import extract  # noqa: E402
from spec import load as load_spec  # noqa: E402
from verify import verify_claim  # noqa: E402

# Verdicts that keep a finding in the report. `unsupported` covers route
# existence, which no request can settle but static analysis already proves;
# `error` covers a gate failure, kept and counted rather than silently dropped
# so an infrastructure problem never looks like a clean bill of health.
KEEP = {"confirmed", "unsupported", "error"}

ALLOWLIST = pathlib.Path(__file__).resolve().parent / "memory" / "allowlist.json"


def load_allowlist(path=ALLOWLIST):
    """Accepted, intentional divergence. Suppressing it keeps repeat runs
    readable; without this, a reviewer learns to scroll past the report, which
    costs more than the noise itself."""
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text()).get("accepted", [])
    except json.JSONDecodeError:
        return []


def allowed(claim, accepted):
    for entry in accepted:
        if (entry.get("path") == claim["path"]
                and str(entry.get("method", "")).lower() == claim["method"]
                and entry.get("kind") == claim["kind"]
                and (entry.get("detail") in (None, "", claim.get("detail")))):
            return True
    return False

print_lock = threading.Lock()


def log(message):
    with print_lock:
        print(message, flush=True)


def case_paths(case_dir):
    case_dir = pathlib.Path(case_dir)
    api = case_dir / "api" if (case_dir / "api").exists() else case_dir
    return api, case_dir / "spec" / "openapi.json"


def agent_pass(api, spec, table, known, model, pool):
    """Run the agent over every endpoint code and spec both describe."""
    keys = sorted(set(spec.keys()) &
                  {(r["path"], r["method"].lower()) for r in table["routes"]})
    jobs = {pool.submit(audit_endpoint, api, spec, key, table, known, model): key
            for key in keys}

    claims, usage = [], {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0}
    errors, unread = [], []
    for job in futures.as_completed(jobs):
        found, used = job.result()
        claims.extend(found)
        for field in ("cost_usd", "input_tokens", "output_tokens", "calls"):
            usage[field] += used.get(field, 0)
        if used.get("error"):
            errors.append(f"{jobs[job][1]} {jobs[job][0]}: {used['error']}")
        if used.get("unread_endpoint"):
            unread.append(used["unread_endpoint"])
    usage["errors"] = errors
    usage["unread_endpoints"] = unread
    return claims, usage


def verify_all(case_dir, spec, claims):
    """Put every claim through the gate.

    Sequential by design: the gate writes a test file into the package under
    audit, so two verifications in one directory would overwrite each other.
    """
    kept, dropped, stats = [], [], {"confirmed": 0, "refuted": 0, "unsupported": 0, "error": 0}
    for claim in claims:
        outcome = verify_claim(case_dir, claim, spec)
        verdict = outcome["verdict"]
        stats[verdict] = stats.get(verdict, 0) + 1
        enriched = dict(claim, verdict=verdict, verification=outcome.get("detail", ""))
        if verdict == "confirmed" and outcome.get("detail"):
            # The gate saw the real behaviour, so its observation belongs in the
            # evidence - appended, not substituted. Replacing the static evidence
            # threw away the file and line the finding was anchored to, which is
            # what an inline CI annotation needs.
            enriched["evidence"] = f"{claim.get('evidence', '')} — verified: {outcome['detail']}"
        (kept if verdict in KEEP else dropped).append(enriched)
    return kept, dropped, stats


def audit_one(case_dir, model, pool, strip_prefix):
    api, spec_path = case_paths(case_dir)
    spec = load_spec(spec_path)
    table = extract(api, strip_prefix=strip_prefix)
    started = time.time()

    mechanical = deterministic_audit(api, spec_path, strip_prefix=strip_prefix)
    judged, usage = agent_pass(api, spec, table, mechanical, model, pool)

    accepted = load_allowlist()
    claims = [c for c in mechanical + judged if not allowed(c, accepted)]
    suppressed = len(mechanical) + len(judged) - len(claims)

    kept, dropped, stats = verify_all(case_dir, spec, claims)

    return {
        "findings": kept,
        "meta": {
            "wall_clock_seconds": round(time.time() - started, 1),
            "cost_usd": round(usage["cost_usd"], 6),
            "human_minutes": 0,
            "model": model,
            "model_calls": usage["calls"],
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "deterministic_claims": len(mechanical),
            "agent_claims": len(judged),
            "suppressed_by_allowlist": suppressed,
            "verification": stats,
            "dropped_by_gate": [
                {k: d[k] for k in ("path", "method", "kind", "detail", "source")}
                for d in dropped
            ],
            "agent_errors": usage["errors"],
            "unread_endpoints": usage["unread_endpoints"],
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", default="eval/cases")
    parser.add_argument("--out", default="reports/runs/agent")
    parser.add_argument("--repo", help="audit a real repository instead of the cases")
    parser.add_argument("--spec", help="openapi.json, with --repo")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--strip-prefix", default="/v1")
    parser.add_argument("--workers", type=int, default=8,
                        help="concurrent model calls (default 8)")
    parser.add_argument("--only", help="comma-separated case ids")
    args = parser.parse_args()

    out = pathlib.Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    pool = futures.ThreadPoolExecutor(max_workers=args.workers)

    if args.repo:
        if not args.spec:
            sys.exit("--repo requires --spec")
        api = pathlib.Path(args.repo).resolve()
        spec = load_spec(args.spec)
        table = extract(api, strip_prefix=args.strip_prefix)
        mechanical = deterministic_audit(api, args.spec, strip_prefix=args.strip_prefix)
        log(f"deterministic: {len(mechanical)} finding(s)")
        judged, usage = agent_pass(api, spec, table, mechanical, args.model, pool)
        log(f"agent: {len(judged)} claim(s), {usage['calls']} calls, ${usage['cost_usd']:.4f}")
        with open(out / "report.json", "w") as f:
            json.dump({"findings": mechanical + judged, "meta": usage}, f, indent=2, default=str)
        log(f"\nwritten to {out / 'report.json'}")
        log("note: findings are unverified — the gate needs a buildable package under test")
        return

    cases = pathlib.Path(args.cases).resolve()
    if not cases.is_dir():
        sys.exit(f"no cases at {cases}. Run: cd eval && python3 inject.py --all")

    selected = sorted(d for d in cases.iterdir() if d.is_dir())
    if args.only:
        wanted = {c.strip() for c in args.only.split(",")}
        selected = [d for d in selected if d.name in wanted]

    log(f"auditing {len(selected)} case(s) with {args.model}, {args.workers} workers\n")
    total_cost, total_calls = 0.0, 0
    started = time.time()

    for case_dir in selected:
        result = audit_one(case_dir, args.model, pool, args.strip_prefix)
        result["case"] = case_dir.name
        with open(out / f"{case_dir.name}.json", "w") as f:
            json.dump(result, f, indent=2)
            f.write("\n")

        meta = result["meta"]
        total_cost += meta["cost_usd"]
        total_calls += meta["model_calls"]
        verification = meta["verification"]
        log(f"  {case_dir.name}  {len(result['findings']):>2} kept  "
            f"(det {meta['deterministic_claims']}, agent {meta['agent_claims']}, "
            f"gate dropped {verification.get('refuted', 0)})  "
            f"{meta['wall_clock_seconds']:>5.0f}s  ${meta['cost_usd']:.5f}")
        for error in meta["agent_errors"]:
            log(f"      ! {error}")

    pool.shutdown()
    log(f"\n{len(selected)} cases, {total_calls} model calls, "
        f"${total_cost:.4f}, {time.time() - started:.0f}s total")
    log(f"written to {out}")


if __name__ == "__main__":
    main()
