#!/usr/bin/env python3
"""Deterministic drift rules — component 3 of the auditor.

Compares the AST-derived route table against the OpenAPI index and reports the
disagreements that can be established without judgment. No model is involved,
so every finding here is reproducible and carries a file and line.

The rules are deliberately conservative. Where the AST cannot settle a question
the rule declines rather than guesses: a missed finding costs recall, which the
agent layer can recover, while a false finding costs precision, which is far
harder to win back once a reviewer stops trusting the report.

    from auditor.tools.diff import audit
    findings = audit("eval/fixture", "eval/fixture/spec/openapi.json", strip_prefix="/v1")

Standalone:

    python3 auditor/tools/diff.py eval/fixture eval/fixture/spec/openapi.json --strip-prefix /v1
"""

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from routes import extract, RouteExtractionError  # noqa: E402
from spec import load as load_spec, SpecError  # noqa: E402

SEVERITY = {
    "route_missing_from_spec": "high",
    "route_missing_from_code": "high",
    "response_field_mismatch": "high",
    "response_type_mismatch": "high",
    "response_header_mismatch": "critical",
    "request_param_mismatch": "high",
    "status_code_mismatch": "medium",
    "undocumented_status": "medium",
    "auth_mismatch": "critical",
}

# Field names whose type changing silently corrupts value rather than just
# breaking a parse. Used to escalate severity, never to create a finding.
MONEY_HINTS = ("amount", "balance", "fee", "available", "ledger", "total", "price", "value")


def finding(path, method, kind, detail, evidence, rule, severity=None, file="", line=0):
    """Every finding carries its location as structured fields.

    The location was originally only inside the evidence prose, which meant a
    consumer had to parse English to place an annotation - and it was silently
    lost the moment anything rewrote the evidence string. An inline annotation on
    the right line is most of this tool's value inside CI, so the location is
    data, not text.
    """
    if not file:
        file, line = _location_from(evidence)
    return {
        "path": path,
        "method": method.lower(),
        "kind": kind,
        "detail": detail,
        "severity": severity or SEVERITY.get(kind, "medium"),
        "evidence": evidence,
        "file": file,
        "line": line,
        "rule": rule,
        "source": "deterministic",
    }


def _location_from(evidence):
    for token in (evidence or "").replace(",", " ").split():
        candidate, _, number = token.rpartition(":")
        if candidate and number.isdigit() and ("/" in candidate or "." in candidate):
            return candidate.lstrip("./"), int(number)
    return "", 0


def auth_header_names(spec):
    """Header names that carry authentication, read from the spec's own
    securitySchemes rather than hardcoded, so this generalises past the fixture."""
    schemes = spec.document.get("components", {}).get("securitySchemes", {})
    names = set()
    for scheme in schemes.values():
        if scheme.get("type") == "apiKey" and scheme.get("in") == "header":
            names.add(scheme.get("name", ""))
        if scheme.get("type") == "http":
            names.add("Authorization")
    names.discard("")
    return names


def comparable_struct(struct):
    """A struct is comparable only when every field's wire name is known and the
    struct name resolves to a single declaration.

    An embedded field flattens into the parent in a way the AST alone cannot
    resolve, and a name declared in two packages cannot be attributed to one
    route. Either way the honest move is to skip: a finding citing the wrong
    declaration is worse than no finding, because it burns the reviewer's trust
    in every other finding alongside it.
    """
    if struct is None or struct.get("ambiguous"):
        return False
    return not any(f["skipped"] and not f["json_name"] for f in struct["fields"])


def wire_fields(struct):
    return {f["json_name"]: f for f in struct["fields"] if f["json_name"] and not f["skipped"]}


def audit(source_dir, spec_path, strip_prefix=""):
    """Run every deterministic rule. Returns a list of findings."""
    table = extract(source_dir, strip_prefix=strip_prefix)
    spec = load_spec(spec_path)

    structs = table["structs"]
    handlers = table["handlers"]
    routes = {(r["path"], r["method"].lower()): r for r in table["routes"]}
    auth_headers = auth_header_names(spec)

    findings = []

    # R1 - registered in code, absent from the spec.
    for key, route in sorted(routes.items()):
        if key in spec:
            continue
        if route["method"] == "ANY":
            continue  # a method-less pattern cannot be matched to one operation
        findings.append(finding(
            route["path"], route["method"], "route_missing_from_spec", "",
            f"{route['file']}:{route['line']} registers {route['method']} {route['path']}"
            f" (handler {route['handler']}), which the spec does not document",
            "R1"))

    # R2 - documented in the spec, not registered in code.
    for key in sorted(spec.keys()):
        if key not in routes:
            path, method = key
            findings.append(finding(
                path, method, "route_missing_from_code", "",
                f"spec documents {method.upper()} {path} but no route registers it"
                f" ({spec.source})",
                "R2"))

    # Per-operation rules. Only where code and spec both describe the endpoint.
    for key in sorted(set(routes) & set(spec.keys())):
        path, method = key
        route = routes[key]
        operation = spec[key]
        facts = handlers.get(route["handler"])
        findings.extend(_operation_rules(path, method, route, operation, spec,
                                         facts, structs, auth_headers))

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["path"], f["method"]))
    return findings


def _operation_rules(path, method, route, operation, spec, facts, structs, auth_headers):
    out = []
    success_codes = spec.success_codes((path, method))

    # R3/R4 - the success response body, field names then field types.
    if success_codes:
        response = operation["responses"][success_codes[0]]
        struct = structs.get(response["schema_name"]) if response["schema_name"] else None
        documented = response["properties"]

        if struct and documented and not response["composed"] and comparable_struct(struct):
            actual = wire_fields(struct)

            missing = sorted(set(documented) - set(actual))
            extra = sorted(set(actual) - set(documented))
            if missing or extra:
                parts = []
                if missing:
                    parts.append(f"spec documents {', '.join(missing)} with no matching field")
                if extra:
                    parts.append(f"code returns {', '.join(extra)} undocumented")
                out.append(finding(
                    path, method, "response_field_mismatch",
                    (missing or extra)[0],
                    f"{struct['file']}:{struct['line']} struct {struct['name']}: " + "; ".join(parts),
                    "R3"))

            for name in sorted(set(documented) & set(actual)):
                expected = documented[name]["type"]
                got = actual[name]["json_type"]
                if not expected or not got or expected == got:
                    continue
                severity = "critical" if any(h in name.lower() for h in MONEY_HINTS) else "high"
                out.append(finding(
                    path, method, "response_type_mismatch", name,
                    f"{struct['file']}:{actual[name]['line']} {struct['name']}.{actual[name]['name']}"
                    f" is {actual[name]['go_type']} (JSON {got}); spec declares {expected}",
                    "R4", severity=severity))

    # Same reasoning as comparable_struct: a handler name declared in more than
    # one package cannot be attributed to this route, so every body-derived rule
    # below declines rather than guessing which declaration to cite.
    if not facts or facts.get("ambiguous"):
        return out

    # R5/R6 - status codes the handler can actually emit.
    documented_codes = set(spec.status_codes((path, method)))
    handler_success = facts["success_code"]
    spec_success = {int(c) for c in success_codes}

    if handler_success and spec_success and handler_success not in spec_success:
        out.append(finding(
            path, method, "status_code_mismatch", str(sorted(spec_success)[0]),
            f"{facts['file']}:{facts['line']} {facts['name']} returns {handler_success}"
            f" on success; spec documents {', '.join(success_codes)}",
            "R6"))

    for code in facts["statuses"]:
        if 200 <= code < 300:
            continue  # handled by R6, which is the more precise statement
        if str(code) in documented_codes:
            continue
        out.append(finding(
            path, method, "undocumented_status", str(code),
            f"{facts['file']}:{facts['line']} {facts['name']} can return {code},"
            f" which the spec does not document",
            "R5"))

    # R7 - query parameters, both directions.
    documented_query = {p["name"] for p in operation["params"] if p["in"] == "query"}
    read_query = set(facts["query_params"])
    if documented_query or read_query:
        undocumented = sorted(read_query - documented_query)
        unread = sorted(documented_query - read_query)
        if undocumented and unread:
            # The strongest signal: one name went out, another came in. Almost
            # always a rename, and the documented name now silently does nothing.
            out.append(finding(
                path, method, "request_param_mismatch", unread[0],
                f"{facts['file']}:{facts['line']} {facts['name']} reads "
                f"{', '.join(undocumented)}; spec documents {', '.join(unread)}"
                f" — the documented parameter is ignored",
                "R7"))
        elif undocumented:
            out.append(finding(
                path, method, "request_param_mismatch", undocumented[0],
                f"{facts['file']}:{facts['line']} {facts['name']} reads "
                f"{', '.join(undocumented)}, undocumented in the spec",
                "R7", severity="medium"))

    # R8 - the handler enforces auth the spec does not declare.
    #
    # One direction only. Auth is usually applied by middleware, so a handler
    # that does not read the header proves nothing about whether the endpoint is
    # protected. The reverse - a handler checking a key on an endpoint the spec
    # calls public - is unambiguous.
    if not operation["security"]:
        enforced = sorted(set(facts["headers_read"]) & auth_headers)
        if enforced:
            out.append(finding(
                path, method, "auth_mismatch", enforced[0],
                f"{facts['file']}:{facts['line']} {facts['name']} requires "
                f"{', '.join(enforced)} but the spec documents this endpoint as public",
                "R8"))

    # R9 - custom response headers the documentation never names.
    described = " ".join([
        operation["description"], operation["summary"],
        *(r["description"] for r in operation["responses"].values()),
    ]).lower()
    declared = {h.lower() for r in operation["responses"].values() for h in r["headers"]}
    for header in facts["headers_set"]:
        if not header.lower().startswith("x-"):
            continue  # only contract-bearing custom headers, not Content-Type
        if header.lower() in declared or header.lower() in described:
            continue
        out.append(finding(
            path, method, "response_header_mismatch", header,
            f"{facts['file']}:{facts['line']} {facts['name']} sets {header},"
            f" which appears nowhere in the documentation for this operation",
            "R9"))

    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", help="directory of Go source")
    parser.add_argument("spec", help="path to openapi.json")
    parser.add_argument("--strip-prefix", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        findings = audit(args.source, args.spec, args.strip_prefix)
    except (RouteExtractionError, SpecError) as exc:
        sys.exit(f"error: {exc}")

    if args.json:
        print(json.dumps({"findings": findings}, indent=2))
        return

    if not findings:
        print("no deterministic drift found")
        return

    print(f"{len(findings)} finding(s)\n")
    for f in findings:
        print(f"[{f['severity']:<8}] {f['method'].upper():<6} {f['path']:<24} "
              f"{f['kind']}  ({f['rule']})")
        print(f"             {f['evidence']}\n")


if __name__ == "__main__":
    main()
