#!/usr/bin/env python3
"""The baseline: one direct prompt per case.

The fair comparison point the brief asks for: a reasonable basic way to handle
the task before any agent design. One model call per case, given the same Go
source and the same specification the auditor sees. No tools, no AST, no
per-endpoint fan-out, no verification gate.

The `kind` vocabulary is handed to it deliberately. Withholding it would make
the baseline lose on formatting rather than on substance, and a baseline that
fails for a preventable reason proves nothing.

    python3 baseline/run.py --cases eval/cases --out reports/runs/baseline
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "auditor"))

from llm import chat, DEFAULT_MODEL, LLMError  # noqa: E402

KINDS = [
    "route_missing_from_spec", "route_missing_from_code",
    "response_field_mismatch", "response_type_mismatch", "response_header_mismatch",
    "request_param_mismatch", "request_required_mismatch",
    "status_code_mismatch", "undocumented_status",
    "auth_mismatch", "validation_mismatch", "default_value_mismatch",
]

SYSTEM = "You review Go HTTP APIs for contract drift. You reply with JSON only."

TEMPLATE = """You are reviewing a Go HTTP API for contract drift.

Below are the API's Go source files and its published OpenAPI specification. The
specification is what external partners integrate against. Find every place where
the code and the specification disagree.

For each disagreement report:
  - "path": the API path as written in the spec, e.g. /payouts/{{id}}
  - "method": the HTTP method, lowercase
  - "kind": one of {kinds}
  - "detail": the specific field, parameter, header or status involved
  - "severity": critical, high, medium or low
  - "evidence": the file and line supporting the finding

Report only genuine disagreements between code and specification. Refactors that
leave the wire contract unchanged are not findings.

Respond with JSON only: {{"findings": [...]}}

=== GO SOURCE ===

{source}

=== OPENAPI SPECIFICATION ===

{spec}
"""


def gather_source(api_dir):
    """Every non-test Go file, concatenated with its path: the whole package,
    which is exactly the point: the baseline gets no help locating anything."""
    parts = []
    for path in sorted(pathlib.Path(api_dir).rglob("*.go")):
        if path.name.endswith("_test.go"):
            continue
        parts.append(f"--- {path.relative_to(api_dir)} ---\n{path.read_text()}")
    return "\n\n".join(parts)


def normalise(findings):
    """Keep the fields the scorer matches on. A malformed entry is dropped
    rather than repaired; inventing structure for the baseline would flatter it."""
    out = []
    for item in findings if isinstance(findings, list) else []:
        if not isinstance(item, dict):
            continue
        path, method, kind = (str(item.get(k, "")).strip() for k in ("path", "method", "kind"))
        if not path or not method or not kind:
            continue
        out.append({
            "path": path, "method": method.lower(), "kind": kind,
            "detail": str(item.get("detail", ""))[:120],
            "severity": str(item.get("severity", "medium")),
            "evidence": str(item.get("evidence", ""))[:400],
            "source": "baseline",
        })
    return out


def run_case(case_dir, model):
    case_dir = pathlib.Path(case_dir)
    api = case_dir / "api" if (case_dir / "api").exists() else case_dir
    spec_path = case_dir / "spec" / "openapi.json"

    prompt = TEMPLATE.format(
        kinds=json.dumps(KINDS),
        source=gather_source(api),
        spec=spec_path.read_text(),
    )

    started = time.time()
    try:
        reply = chat(model, SYSTEM, prompt, max_tokens=16000)
    except LLMError as exc:
        return case_dir.name, {
            "case": case_dir.name, "findings": [],
            "meta": {"error": str(exc), "cost_usd": 0.0,
                     "wall_clock_seconds": round(time.time() - started, 1),
                     "human_minutes": 0, "model": model},
        }

    findings = normalise((reply.json or {}).get("findings"))
    return case_dir.name, {
        "case": case_dir.name,
        "findings": findings,
        "meta": {
            "wall_clock_seconds": round(reply.elapsed, 1),
            "cost_usd": round(reply.cost_usd, 6),
            "human_minutes": 0,
            "model": model,
            "model_calls": 1,
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
            "parsed": reply.json is not None,
            "truncated": reply.truncated,
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", default="eval/cases")
    parser.add_argument("--out", default="reports/runs/baseline")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    cases = pathlib.Path(args.cases).resolve()
    if not cases.is_dir():
        sys.exit(f"no cases at {cases}. Run: cd eval && python3 inject.py --all")
    out = pathlib.Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    selected = sorted(d for d in cases.iterdir() if d.is_dir())
    print(f"baseline over {len(selected)} case(s) with {args.model}\n")

    total_cost = 0.0
    started = time.time()
    with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = [pool.submit(run_case, d, args.model) for d in selected]
        for job in futures.as_completed(jobs):
            name, result = job.result()
            with open(out / f"{name}.json", "w") as f:
                json.dump(result, f, indent=2)
                f.write("\n")
            meta = result["meta"]
            total_cost += meta["cost_usd"]
            note = f"  [{meta['error']}]" if meta.get("error") else (
                "" if meta.get("parsed") else "  [unparseable reply]")
            print(f"  {name}  {len(result['findings']):>2} finding(s)  "
                  f"{meta['wall_clock_seconds']:>5.0f}s  ${meta['cost_usd']:.5f}{note}")

    print(f"\n{len(selected)} cases, ${total_cost:.4f}, {time.time() - started:.0f}s")
    print(f"written to {out}")


if __name__ == "__main__":
    main()
