#!/usr/bin/env python3
"""Per-endpoint auditor agent — component 6.

Reads one endpoint's handler source against its specification and reports drift
the deterministic rules cannot settle. Those rules already catch what is
mechanical; this exists for the residue that needs judgment — a handler that
rejects a field the spec calls optional, a default that quietly changed, a
validation bound loosened below what is documented.

Two constraints shape everything here:

**One endpoint per call.** The unit the contract is expressed in is the
endpoint, and one handler plus one operation fits comfortably in context with
room to reason. Sending the whole package invites the model to answer about the
wrong function.

**The model proposes; it does not decide.** Every claim returned here is a
candidate. It goes to the verification gate, and where the gate can execute it,
a claim that fails to reproduce is dropped. This module's job is recall within a
strict vocabulary — precision is enforced downstream.

    from auditor.audit_endpoint import audit_endpoint
    claims = audit_endpoint(api_dir, spec, ("/refunds", "post"), table, known=[])
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "tools"))

from llm import chat, DEFAULT_MODEL, LLMError  # noqa: E402
from routes import extract  # noqa: E402
from spec import load as load_spec  # noqa: E402

# The kinds this layer may report. Deliberately excludes everything the
# deterministic rules own: route existence, response shapes, status codes, query
# parameter names, auth and response headers are all settled by parsing, and
# inviting the model to re-report them only creates duplicates and disagreement.
JUDGMENT_KINDS = {
    "request_required_mismatch":
        "the handler rejects a request that the specification says is valid — it "
        "enforces a field, header or condition the spec does not mark as required",
    "default_value_mismatch":
        "the handler applies a different default from the one the spec documents "
        "for a parameter the caller omitted",
    "validation_mismatch":
        "the handler enforces a different bound, length, format or enum than the "
        "constraint written in the spec",
}

SYSTEM = """You review one HTTP endpoint at a time for contract drift between Go handler code and the OpenAPI specification an external partner integrates against.

You report only drift that a reader can confirm by looking at a specific line of the handler. You are not reviewing code quality, naming, style, error handling, security or performance. A refactor that leaves the wire behaviour unchanged is not drift.

Silence is a correct and common answer. Most endpoints have no drift of the kinds you are asked about. Reporting nothing is far better than reporting something you are not certain of: a false finding costs a reviewer more than a missed one, because it makes them distrust every other finding beside it.

You reply with JSON only."""

TEMPLATE = """# Endpoint

{method} {path}

# Handler source

Function `{handler}` at {file}:{start}-{end}

```go
{source}
```

# What the specification promises

{spec_json}

# Already reported by static analysis — do NOT repeat these

{known}

# Your task

Report only drift of these kinds:

{kinds}

Nothing else is in scope. Route existence, response body fields and types, status
codes, query parameter names, authentication and response headers are all handled
by static analysis already and must not be reported here.

For each finding give:
  - "path": exactly "{path}"
  - "method": exactly "{method}"
  - "kind": one of {kind_list}
  - "detail": the specific field, parameter or constraint involved
  - "evidence": the handler line that shows it, quoted, with what the spec says instead
  - "confidence": "high" only if the handler line is unambiguous, otherwise "low"

Report a finding only when the handler line and the specification directly
contradict each other. If the handler does something the spec is simply silent
about, that is not drift of these kinds — omit it.

Reply with JSON only:

{{"findings": []}}"""


def handler_source(api_dir, facts):
    """Slice one function out of its file. Sending the whole package instead
    invites the model to answer about a neighbouring handler."""
    path = pathlib.Path(api_dir) / facts["file"]
    if not path.exists():
        return ""
    lines = path.read_text().splitlines()
    start = max(facts["line"] - 1, 0)
    end = min(facts.get("end_line") or facts["line"], len(lines))
    return "\n".join(lines[start:end])


def spec_summary(operation):
    """The parts of the operation a judgment-kind finding could contradict.
    Response bodies are omitted on purpose — they belong to the deterministic
    rules, and including them here draws the model toward out-of-scope reports."""
    return {
        "summary": operation["summary"],
        "description": operation["description"],
        # Every key, including validation constraints. An earlier version
        # whitelisted five fields and silently dropped minLength/maxLength/
        # pattern/enum, which produced a confident finding against code that was
        # honouring the spec exactly.
        "parameters": operation["params"],
        "request_body_required": operation["request_required"],
        "request_body_properties": operation["request_properties"],
        "security": operation["security"],
    }


def build_prompt(path, method, facts, operation, source, known):
    known_text = "\n".join(
        f"- {f['kind']}: {f.get('detail') or ''} ({f.get('evidence', '')[:110]})"
        for f in known
    ) or "- (nothing)"
    kinds_text = "\n".join(f'  - "{k}" — {v}' for k, v in JUDGMENT_KINDS.items())
    return TEMPLATE.format(
        method=method.lower(), path=path, handler=facts["name"],
        file=facts["file"], start=facts["line"], end=facts.get("end_line", facts["line"]),
        source=source,
        spec_json=json.dumps(spec_summary(operation), indent=2),
        known=known_text, kinds=kinds_text,
        kind_list=json.dumps(sorted(JUDGMENT_KINDS)),
    )


def clean_claims(raw, path, method, drop_low_confidence=True, file="", line=0):
    """Keep only claims that are in vocabulary and about this endpoint.

    A model that invents a kind, or answers about a different endpoint, has
    drifted from the task; those replies are dropped here rather than passed on
    to be argued with downstream. Low-confidence claims are dropped by default —
    the gate cannot execute these kinds, so an unverifiable guess would reach the
    report unchallenged.
    """
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip()
        if kind not in JUDGMENT_KINDS:
            continue
        if str(item.get("path", "")).strip() != path:
            continue
        if str(item.get("method", "")).strip().lower() != method.lower():
            continue
        if drop_low_confidence and str(item.get("confidence", "")).lower() == "low":
            continue
        out.append({
            "path": path,
            "method": method.lower(),
            "kind": kind,
            "detail": str(item.get("detail", ""))[:120],
            "severity": "high" if kind == "request_required_mismatch" else "medium",
            "evidence": str(item.get("evidence", ""))[:400],
            "file": file,
            "line": line,
            "rule": "agent",
            "source": "agent",
            "confidence": str(item.get("confidence", "")).lower() or "unstated",
        })
    return out


def audit_endpoint(api_dir, spec, key, table, known=None, model=DEFAULT_MODEL):
    """Audit one endpoint. Returns (claims, usage)."""
    path, method = key
    known = known or []
    usage = {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "elapsed": 0.0, "calls": 0}

    route = next((r for r in table["routes"]
                  if r["path"] == path and r["method"].lower() == method.lower()), None)
    if route is None:
        return [], usage
    facts = table["handlers"].get(route["handler"])
    if not facts or facts.get("ambiguous"):
        return [], usage

    source = handler_source(api_dir, facts)
    if not source:
        return [], usage

    prompt = build_prompt(path, method, facts, spec[key], source,
                          [f for f in known if f["path"] == path and f["method"] == method.lower()])

    try:
        reply = chat(model, SYSTEM, prompt)
    except LLMError as exc:
        usage["error"] = str(exc)
        return [], usage

    usage.update(cost_usd=reply.cost_usd, input_tokens=reply.input_tokens,
                 output_tokens=reply.output_tokens, elapsed=reply.elapsed, calls=1)
    if reply.truncated and reply.json is None:
        usage["error"] = "reply truncated before any content"
        return [], usage
    if reply.json is None:
        usage["error"] = "reply was not JSON"
        return [], usage

    # Anchor every agent claim to the handler it actually read.
    return clean_claims(reply.json.get("findings"), path, method,
                        file=facts["file"], line=facts["line"]), usage


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("case", help="case directory, or eval/fixture for the clean baseline")
    parser.add_argument("--endpoint", help='e.g. "post /refunds" (default: every endpoint)')
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--strip-prefix", default="/v1")
    args = parser.parse_args()

    case = pathlib.Path(args.case).resolve()
    api_dir = case / "api" if (case / "api").exists() else case
    spec = load_spec(case / "spec" / "openapi.json")
    table = extract(api_dir, strip_prefix=args.strip_prefix)

    if args.endpoint:
        method, path = args.endpoint.split(None, 1)
        keys = [(path.strip(), method.strip().lower())]
    else:
        keys = sorted(set(spec.keys()) &
                      {(r["path"], r["method"].lower()) for r in table["routes"]})

    total = {"cost_usd": 0.0, "calls": 0}
    for key in keys:
        claims, usage = audit_endpoint(api_dir, spec, key, table, model=args.model)
        total["cost_usd"] += usage["cost_usd"]
        total["calls"] += usage["calls"]
        marker = f"{len(claims)} claim(s)" if claims else "—"
        note = f"  [{usage['error']}]" if usage.get("error") else ""
        print(f"  {key[1].upper():<6} {key[0]:<24} {marker}{note}")
        for claim in claims:
            print(f"           {claim['kind']}: {claim['detail']}")
            print(f"           {claim['evidence'][:150]}")

    print(f"\n{total['calls']} call(s), ${total['cost_usd']:.6f}")


if __name__ == "__main__":
    main()
