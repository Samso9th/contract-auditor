#!/usr/bin/env python3
"""Per-endpoint auditor agent: component 6.

Reads one endpoint's handler source against its specification and reports drift
the deterministic rules cannot settle. Those rules already catch what is
mechanical; this exists for the residue that needs judgment: a handler that
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
strict vocabulary; precision is enforced downstream.

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

from llm import chat, DEFAULT_MODEL, DEFAULT_MAX_TOKENS, LLMError  # noqa: E402
from diff import extract  # noqa: E402
import languages  # noqa: E402
from spec import load as load_spec  # noqa: E402

# The kinds this layer may report. Deliberately excludes everything the
# deterministic rules own: route existence, response shapes, status codes, query
# parameter names, auth and response headers are all settled by parsing, and
# inviting the model to re-report them only creates duplicates and disagreement.
ALL_KINDS = {
    "response_field_mismatch":
        "the response body omits a field the specification promises, or carries one it does not document",
    "response_type_mismatch":
        "a response field's JSON type differs from the documented type",
    "response_header_mismatch":
        "a response header the documentation names is not the header the handler sets",
    "request_param_mismatch":
        "the handler reads a query parameter under a different name than the one documented",
    "status_code_mismatch":
        "the handler's success status differs from the documented one",
    "undocumented_status":
        "the handler can return a status the specification does not document",
    "auth_mismatch":
        "the handler enforces authentication on an endpoint the specification documents as public",
}

JUDGMENT_KINDS = {
    "request_required_mismatch":
        "the handler rejects a request that the specification says is valid. It "
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

# Already reported by static analysis. Do NOT repeat these

{known}
{memory}
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
about, that is not drift of these kinds: omit it.

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


def spec_summary(operation, kinds=None):
    """The parts of the operation a finding could contradict.

    Response bodies are included only when the vocabulary allows a response-shaped
    finding. For Go they are omitted - the deterministic rules own them, and
    showing them invites out-of-scope reports. For TypeScript, whose rules cannot
    settle them, omitting them asks the agent about a shape it has never seen,
    and it correctly reports nothing.
    """
    kinds = kinds or {}
    summary = {
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

    if any(k.startswith("response_") or k.endswith("_status") or k == "status_code_mismatch"
           for k in kinds):
        summary["responses"] = {
            code: {
                "description": response.get("description", ""),
                "properties": response.get("properties", {}),
                "headers": response.get("headers", []),
            }
            for code, response in operation["responses"].items()
        }
    return summary


def build_prompt(path, method, facts, operation, source, known, kinds=None,
                 recalled=""):
    known_text = "\n".join(
        f"- {f['kind']}: {f.get('detail') or ''} ({f.get('evidence', '')[:110]})"
        for f in known
    ) or "- (nothing)"
    kinds = kinds or JUDGMENT_KINDS
    kinds_text = "\n".join(f'  - "{k}": {v}' for k, v in kinds.items())
    # Recalled negatives sit between what static analysis already found and the
    # task, which is where a reader would want them: after the facts, before the
    # judgment. Empty when there is no history, so a first run's prompt is byte
    # for byte the prompt this agent has always sent.
    return TEMPLATE.format(
        method=method.lower(), path=path, handler=facts["name"],
        file=facts["file"], start=facts["line"], end=facts.get("end_line", facts["line"]),
        source=source,
        spec_json=json.dumps(spec_summary(operation, kinds), indent=2),
        known=known_text, kinds=kinds_text,
        memory=f"\n{recalled}\n" if recalled else "",
        kind_list=json.dumps(sorted(kinds)),
    )


def clean_claims(raw, path, method, drop_low_confidence=True, file="", line=0,
                 kinds=None):
    """Keep only claims that are in vocabulary and about this endpoint.

    A model that invents a kind, or answers about a different endpoint, has
    drifted from the task; those replies are dropped here rather than passed on
    to be argued with downstream. Low-confidence claims are dropped by default,
    the gate cannot execute these kinds, so an unverifiable guess would reach the
    report unchallenged.
    """
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip()
        if kind not in (kinds or JUDGMENT_KINDS):
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


def vocabulary(adapter=None):
    """What this language's agent may report.

    Always the three judgment kinds, plus anything the language's deterministic
    rules cannot settle. Go's rules cover response shapes and status codes from
    the AST, so its agent must not restate them; TypeScript's do not, so its
    agent may. The gate verifies both the same way, which is what makes widening
    the vocabulary safe rather than a precision risk.
    """
    kinds = dict(JUDGMENT_KINDS)
    if adapter is not None:
        settled = getattr(adapter, "DETERMINISTIC_KINDS", set())
        kinds.update({k: v for k, v in ALL_KINDS.items() if k not in settled})
    return kinds


def audit_endpoint(api_dir, spec, key, table, known=None, model=DEFAULT_MODEL,
                   adapter=None, memory=None):
    """Audit one endpoint. Returns (claims, usage).

    `memory` is optional learned history (see auditor/memory). It can add
    refuted precedents to the prompt and nothing else: it cannot filter a claim
    on the way out, because the gate downstream is the only thing entitled to
    decide that.
    """
    path, method = key
    known = known or []
    usage = {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "elapsed": 0.0, "calls": 0}

    route = next((r for r in table["routes"]
                  if r["path"] == path and r["method"].lower() == method.lower()), None)
    if route is None:
        return [], usage
    # Same location-keyed lookup the rules use. Keyed only by name, a route
    # function and the service it delegates to collide, the ambiguity guard fires,
    # and this returned silently - no call, no claim, no error. Eight endpoints
    # per case went unaudited and the run still reported as complete.
    facts = (table.get("handlers_by_location") or {}).get(
        f"{route['file']}::{route['handler']}") or (table.get("handlers") or {}).get(route["handler"])
    if facts and facts.get("ambiguous"):
        usage["error"] = (f"handler {route['handler']!r} is declared in more than one "
                          f"file; declining rather than auditing the wrong one")
        usage["unread_endpoint"] = f"{method.upper()} {path}"
        return [], usage

    if facts:
        source = handler_source(api_dir, facts)
        location = (facts["file"], facts["line"])
    elif adapter is not None and hasattr(adapter, "handler_source"):
        # Languages whose extractor emits no handler facts still have a handler;
        # the adapter knows how to find it. Returning early here is what made the
        # TypeScript agent silently audit nothing.
        source, file, line = adapter.handler_source(api_dir, route, table)
        location = (file, line)
    else:
        source, location = None, (None, 0)

    # The behaviour being audited is frequently not in the function the route
    # registers. A thin route handler delegating to a service shows the agent
    # nothing, and it then reports a clean endpoint truthfully but uselessly.
    # Appended for every language whose adapter can find them, not just the
    # branch above - that asymmetry cost every Python judgment finding.
    if source and location[0] and adapter is not None \
            and hasattr(adapter, "supporting_sources"):
        for label, body in adapter.supporting_sources(api_dir, location[0]):
            source += f"\n\n# ---- imported by {location[0]}: {label} ----\n{body}"

    if not source:
        usage["error"] = f"could not locate the source of handler {route['handler']!r}"
        usage["unread_endpoint"] = f"{method.upper()} {path}"
        return [], usage

    kinds = vocabulary(adapter)
    described = facts or {"name": route["handler"], "file": location[0],
                          "line": location[1], "end_line": location[1]}
    recalled = memory.prompt_block(path, method, set(kinds)) if memory is not None else ""
    if recalled:
        usage["recalled"] = True
    prompt = build_prompt(path, method, described, spec[key], source,
                          [f for f in known if f["path"] == path and f["method"] == method.lower()],
                          kinds, recalled)

    # An unparseable reply is indistinguishable from "no drift here": both yield
    # zero claims. That makes a transport-level failure look like a clean
    # endpoint, which is the one wrong answer this tool must never give. Retry
    # once with a larger budget, then record the failure loudly.
    reply = None
    for attempt in range(2):
        try:
            reply = chat(model, SYSTEM, prompt,
                         max_tokens=DEFAULT_MAX_TOKENS * (attempt + 1))
        except LLMError as exc:
            # Any failure to read an endpoint marks it unread, not just an
            # unparseable reply. A DNS failure or a rate limit produces zero
            # claims exactly like a clean endpoint does, and without this the
            # difference never reaches the report.
            usage["error"] = str(exc)
            usage["unread_endpoint"] = f"{method.upper()} {path}"
            return [], usage

        usage["calls"] += 1
        usage["cost_usd"] += reply.cost_usd
        usage["input_tokens"] += reply.input_tokens
        usage["output_tokens"] += reply.output_tokens
        usage["elapsed"] += reply.elapsed

        if reply.json is not None:
            break
        usage["retried"] = True

    if reply is None or reply.json is None:
        usage["error"] = ("reply truncated before any content"
                          if reply is not None and reply.truncated
                          else "reply was not JSON after retry")
        # An endpoint the agent could not read is not an endpoint with no drift.
        # Surfaced so a run with unread endpoints cannot be mistaken for a clean one.
        usage["unread_endpoint"] = f"{method.upper()} {path}"
        return [], usage

    # Anchor every agent claim to the handler it actually read.
    return clean_claims(reply.json.get("findings"), path, method,
                        file=location[0] or "", line=location[1] or 0,
                        kinds=kinds), usage


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("case", help="case directory, or eval/fixture for the clean baseline")
    parser.add_argument("--endpoint", help='e.g. "post /refunds" (default: every endpoint)')
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--strip-prefix", default="/v1")
    parser.add_argument("--language", default=None, help="go or typescript; detected when omitted")
    args = parser.parse_args()

    case = pathlib.Path(args.case).resolve()
    api_dir = case / "api" if (case / "api").exists() else case
    spec = load_spec(case / "spec" / "openapi.json")
    adapter = languages.get(args.language) if args.language else languages.detect(api_dir)
    table = extract(api_dir, strip_prefix=args.strip_prefix, language=args.language)

    if args.endpoint:
        method, path = args.endpoint.split(None, 1)
        keys = [(path.strip(), method.strip().lower())]
    else:
        keys = sorted(set(spec.keys()) &
                      {(r["path"], r["method"].lower()) for r in table["routes"]})

    total = {"cost_usd": 0.0, "calls": 0}
    for key in keys:
        claims, usage = audit_endpoint(api_dir, spec, key, table, model=args.model,
                                       adapter=adapter)
        total["cost_usd"] += usage["cost_usd"]
        total["calls"] += usage["calls"]
        marker = f"{len(claims)} claim(s)" if claims else "none"
        note = f"  [{usage['error']}]" if usage.get("error") else ""
        print(f"  {key[1].upper():<6} {key[0]:<24} {marker}{note}")
        for claim in claims:
            print(f"           {claim['kind']}: {claim['detail']}")
            print(f"           {claim['evidence'][:150]}")

    print(f"\n{total['calls']} call(s), ${total['cost_usd']:.6f}")


if __name__ == "__main__":
    main()
