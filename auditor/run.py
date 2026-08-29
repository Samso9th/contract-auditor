#!/usr/bin/env python3
"""The contract auditor: deterministic rules, agent, verification gate.

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
from diff import extract  # noqa: E402
import languages  # noqa: E402
from spec import load as load_spec  # noqa: E402
from verify import verify_claim  # noqa: E402
from memory import ledger, recall  # noqa: E402

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


def agent_pass(api, spec, table, known, model, pool, adapter=None, memory=None,
               repo=""):
    """Run the agent over every endpoint code and spec both describe.

    Where `memory` is supplied, most endpoints are audited with refuted
    precedents in the prompt. A small random sample - epsilon exploration - is
    audited with memory switched off, so the classes memory has learned to
    distrust keep generating evidence. Without that sample, a wrong prior can
    never be contradicted: no claims means no counter-evidence, and the belief
    becomes permanent.
    """
    keys = sorted(set(spec.keys()) &
                  {(r["path"], r["method"].lower()) for r in table["routes"]})

    explored = []
    jobs = {}
    for key in keys:
        endpoint_memory = memory
        if memory is not None and memory.explore(repo, key[0], key[1]):
            endpoint_memory = memory.without_memory()
            explored.append(f"{key[1].upper()} {key[0]}")
        jobs[pool.submit(audit_endpoint, api, spec, key, table, known, model,
                         adapter, endpoint_memory)] = key

    claims, usage = [], {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0}
    errors, unread, recalled = [], [], 0
    for job in futures.as_completed(jobs):
        found, used = job.result()
        claims.extend(found)
        for field in ("cost_usd", "input_tokens", "output_tokens", "calls"):
            usage[field] += used.get(field, 0)
        if used.get("error"):
            errors.append(f"{jobs[job][1]} {jobs[job][0]}: {used['error']}")
        if used.get("unread_endpoint"):
            unread.append(used["unread_endpoint"])
        if used.get("recalled"):
            recalled += 1
    usage["errors"] = errors
    usage["unread_endpoints"] = unread
    usage["endpoints_with_recall"] = recalled
    usage["explored_without_memory"] = sorted(explored)
    return claims, usage


def adapter_test_name(language, claim):
    """A filename that identifies which finding a shipped test belongs to."""
    slug = f"{claim['method']}_{claim['path']}".strip("/").replace("/", "_")
    slug = "".join(c if c.isalnum() or c in "_-" else "_" for c in slug)
    suffix = {"go": "_test.go", "typescript": ".test.mjs",
              "python": "_check.py", "php": "_check.php"}.get(language, ".txt")
    return f"{claim['kind']}__{slug}{suffix}"


def verify_all(case_dir, spec, claims, language=None, strip_prefix="/v1",
               memory=None, ledger_rows=None, repo="", run_id=""):
    """Put every claim through the gate.

    Sequential by design: the gate writes a test file into the package under
    audit, so two verifications in one directory would overwrite each other.

    Every verdict is also written to the claim ledger, refuted ones included.
    Those are the labelled false positives the whole self-improvement scheme
    runs on, and they exist only at this moment: once a claim is dropped from
    the report, reconstructing it means paying for the run again.
    """
    kept, dropped, stats = [], [], {"confirmed": 0, "refuted": 0, "unsupported": 0, "error": 0}
    for claim in claims:
        outcome = verify_claim(case_dir, claim, spec, language=language,
                               strip_prefix=strip_prefix)
        # Calibration spends its budget here. A gate error on a kind that
        # usually confirms is more likely to be losing a real finding than a
        # gate error on a kind that usually refutes, so only the former is worth
        # a second run of the test.
        if outcome["verdict"] == "error" and memory is not None \
                and memory.budget(claim.get("kind", ""))["gate_retries"]:
            outcome = verify_claim(case_dir, claim, spec, language=language,
                                   strip_prefix=strip_prefix)
        verdict = outcome["verdict"]
        if ledger_rows is not None:
            ledger_rows.append(ledger.entry(
                claim, verdict, repo=repo, run_id=run_id,
                memory="off" if memory is None else "on",
                verification=outcome.get("detail", "")))
        stats[verdict] = stats.get(verdict, 0) + 1
        enriched = dict(claim, verdict=verdict, verification=outcome.get("detail", ""))
        # The generated test is the most useful thing a fixing agent can be
        # handed: it already encodes what "fixed" means, so the agent can run it
        # rather than be told. Kept on confirmed findings so the brief can ship
        # them; refuted claims carry nothing worth keeping.
        if verdict == "confirmed" and outcome.get("test"):
            enriched["test_source"] = outcome["test"]
            enriched["test_filename"] = adapter_test_name(language, claim)
        if verdict == "confirmed" and outcome.get("detail"):
            # The gate saw the real behaviour, so its observation belongs in the
            # evidence - appended, not substituted. Replacing the static evidence
            # threw away the file and line the finding was anchored to, which is
            # what an inline CI annotation needs.
            enriched["evidence"] = f"{claim.get('evidence', '')} (verified: {outcome['detail']})"
        (kept if verdict in KEEP else dropped).append(enriched)
    return kept, dropped, stats


def audit_one(case_dir, model, pool, strip_prefix, language=None, memory=None,
              ledger_rows=None, run_id=""):
    api, spec_path = case_paths(case_dir)
    spec = load_spec(spec_path)
    adapter = languages.get(language) if language else languages.detect(api)
    table = extract(api, strip_prefix=strip_prefix, language=language)
    started = time.time()
    repo = pathlib.Path(case_dir).name

    mechanical = deterministic_audit(api, spec_path, strip_prefix=strip_prefix,
                                     language=language)
    judged, usage = agent_pass(api, spec, table, mechanical, model, pool, adapter,
                               memory, repo)

    accepted = load_allowlist()
    claims = [c for c in mechanical + judged if not allowed(c, accepted)]
    suppressed = len(mechanical) + len(judged) - len(claims)

    kept, dropped, stats = verify_all(case_dir, spec, claims, language, strip_prefix,
                                      memory, ledger_rows, repo, run_id)

    # Priors reorder what survived; they never remove anything from it. The
    # confirm rate behind each position is written into the finding so a reader
    # can see why it ranks where it does.
    if memory is not None:
        kept = memory.annotate(kept)

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
            "memory": {
                "enabled": memory is not None,
                "ledger_claims": len(memory.rows) if memory is not None else 0,
                "endpoints_with_recall": usage.get("endpoints_with_recall", 0),
                "explored_without_memory": usage.get("explored_without_memory", []),
                "rules_active": len(memory.rules) if memory is not None else 0,
            },
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
    parser.add_argument("--language", default=None,
                        help="go or typescript; detected when omitted")
    parser.add_argument("--workers", type=int, default=8,
                        help="concurrent model calls (default 8)")
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--no-memory", action="store_true",
                        help="audit with no learned history at all (still writes the ledger)")
    parser.add_argument("--epsilon", type=float, default=recall.EPSILON,
                        help="fraction of endpoints audited with memory off (default 0.05)")
    parser.add_argument("--ledger", default=str(ledger.LEDGER),
                        help="where the claim ledger is appended")
    args = parser.parse_args()

    out = pathlib.Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    pool = futures.ThreadPoolExecutor(max_workers=args.workers)

    # The ledger is written whatever happens, including under --no-memory: a run
    # that consults nothing still produces labelled data, and that is the half of
    # the mechanism that cannot be recovered afterwards.
    run_id = ledger.new_run_id()
    ledger_rows = []
    memory = None if args.no_memory else recall.Memory.load(args.ledger,
                                                            epsilon=args.epsilon)

    if args.repo:
        if not args.spec:
            sys.exit("--repo requires --spec")
        api = pathlib.Path(args.repo).resolve()
        spec = load_spec(args.spec)
        table = extract(api, strip_prefix=args.strip_prefix, language=args.language)
        mechanical = deterministic_audit(api, args.spec, strip_prefix=args.strip_prefix,
                                         language=args.language)
        log(f"deterministic: {len(mechanical)} finding(s)")
        adapter = languages.get(args.language) if args.language else languages.detect(api)
        judged, usage = agent_pass(api, spec, table, mechanical, args.model, pool,
                                   adapter, memory, api.name)
        log(f"agent: {len(judged)} claim(s), {usage['calls']} calls, ${usage['cost_usd']:.4f}")
        with open(out / "report.json", "w") as f:
            json.dump({"findings": mechanical + judged, "meta": usage}, f, indent=2, default=str)
        log(f"\nwritten to {out / 'report.json'}")
        log("note: findings are unverified; the gate needs a buildable package under test")
        return

    cases = pathlib.Path(args.cases).resolve()
    if not cases.is_dir():
        sys.exit(f"no cases at {cases}. Run: cd eval && python3 inject.py --all")

    selected = sorted(d for d in cases.iterdir() if d.is_dir())
    if args.only:
        wanted = {c.strip() for c in args.only.split(",")}
        selected = [d for d in selected if d.name in wanted]

    log(f"auditing {len(selected)} case(s) with {args.model}, {args.workers} workers")
    if memory is not None:
        stats = memory.stats()
        log(f"memory: {stats['claims']} past claim(s) over {stats['runs']} run(s), "
            f"{stats['refuted_available']} refuted available as negatives, "
            f"{stats['rules']} rule(s), epsilon {args.epsilon:.0%}")
    else:
        log("memory: disabled for this run")
    log("")
    total_cost, total_calls, total_unread = 0.0, 0, 0
    started = time.time()

    for case_dir in selected:
        case_rows = []
        result = audit_one(case_dir, args.model, pool, args.strip_prefix, args.language,
                           memory, case_rows, run_id)
        # Flushed per case, not at the end of the run. A run that dies on case
        # nine must not take the labelled data from cases one to eight with it -
        # that data cost model calls and gate runs to produce, and is the one
        # thing here that cannot be reconstructed afterwards.
        ledger.record(case_rows, args.ledger)
        ledger_rows.extend(case_rows)
        result["case"] = case_dir.name
        with open(out / f"{case_dir.name}.json", "w") as f:
            json.dump(result, f, indent=2)
            f.write("\n")

        meta = result["meta"]
        total_cost += meta["cost_usd"]
        total_calls += meta["model_calls"]
        total_unread += len(meta.get("unread_endpoints", []))
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
    if ledger_rows:
        refuted = sum(1 for row in ledger_rows if row["verdict"] == "refuted")
        log(f"ledger: +{len(ledger_rows)} claim(s) ({refuted} refuted, kept as "
            f"negatives for the next run) -> {args.ledger}")

    if total_unread:
        # Stated at the end, where it cannot be missed. A run that could not read
        # part of the API is not a run that found nothing wrong there, and the
        # two look identical in the findings alone.
        log(f"\n::warning::{total_unread} endpoint(s) could not be audited. "
            f"This run is incomplete - the findings do not cover them.")


if __name__ == "__main__":
    main()
