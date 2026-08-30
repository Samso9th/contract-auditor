#!/usr/bin/env python3
"""Deterministic drift rules: component 3 of the auditor.

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
import fnmatch
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from routes import extract as go_extract, RouteExtractionError  # noqa: E402
from spec import load as load_spec, SpecError  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def extract(source_dir, strip_prefix="", language=None):
    """Route table for a project in any supported language.

    Falls back to the Go extractor when the adapter layer is unavailable, so
    this module keeps working standalone.
    """
    try:
        import languages
    except ImportError:
        return go_extract(source_dir, strip_prefix=strip_prefix)

    adapter = languages.get(language) if language else languages.detect(source_dir)
    if adapter is None:
        raise RouteExtractionError(
            f"could not identify the language of {source_dir}. "
            f"Pass one explicitly: {', '.join(languages.names())}")
    return adapter.extract(source_dir, strip_prefix=strip_prefix)

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


def type_conflicts(observed, documented):
    """Whether an observed JSON type genuinely contradicts the documented one.

    JSON has no integer type: `1` is a number on the wire, and a language that
    does not distinguish them cannot be said to disagree with a spec that
    declares `integer`. Go can be held to the stricter reading because its
    extractor reports `int` and `float64` separately; a JavaScript or Python
    literal cannot. Reporting that as drift would be flagging the format's own
    limitation as a defect.
    """
    if not observed or not documented or observed == documented:
        return False
    numeric = {"number", "integer"}
    if observed in numeric and documented in numeric:
        return False
    return True


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


def parse_excludes(raw):
    """Split an exclude-paths value into patterns.

    Accepts newlines or commas so a workflow can write either the block form or
    a one-liner. A pattern is matched against the path as the spec would write
    it, which is after --strip-prefix has been removed.
    """
    if not raw:
        return ()
    if isinstance(raw, (list, tuple)):
        parts = list(raw)
    else:
        parts = [p for chunk in str(raw).splitlines() for p in chunk.split(",")]
    out = []
    for part in parts:
        pattern = part.strip()
        if not pattern or pattern.startswith("#"):
            continue
        # A pattern without a leading slash is what people write first, and
        # rejecting it teaches nothing that accepting it would not.
        out.append(pattern if pattern.startswith("/") else "/" + pattern)
    return tuple(out)


def parse_names(raw):
    """Split a newline or comma separated list of identifiers."""
    if not raw:
        return ()
    parts = raw if isinstance(raw, (list, tuple)) else [
        p for chunk in str(raw).splitlines() for p in chunk.split(",")]
    return tuple(p.strip() for p in parts if p.strip() and not p.strip().startswith("#"))


def path_excluded(path, patterns):
    """fnmatch, so `*` crosses slashes: /auth/* covers /auth/me/password.

    A trailing /* also covers the collection itself, so /auth/* excludes /auth.
    Writing the subtree and then still being reported on its root is nobody's
    intent, and the alternative is every caller listing both forms.
    """
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        if pattern.endswith("/*") and path == pattern[:-2]:
            return True
    return False


def audit(source_dir, spec_path, strip_prefix="", language=None, exclude=(),
          contract_middleware=()):
    """Run every deterministic rule. Returns a list of findings.

    A language whose adapter supplies no handler facts - TypeScript today - gets
    the route-existence rules and nothing more. The body rules skip rather than
    guess, so the findings that do appear are as trustworthy as they are in Go.
    """
    table = extract(source_dir, strip_prefix=strip_prefix, language=language)
    spec = load_spec(spec_path)

    structs = table.get("structs") or {}
    handlers = table.get("handlers") or {}
    extracted = table.get("routes") or []

    # No routes at all is never a clean bill of health - it means the source
    # directory, language or layout is wrong. Saying so beats reporting that
    # every documented endpoint is missing from the code.
    if not extracted:
        raise RouteExtractionError(
            f"no routes found in {source_dir}. Check that --source-dir points at "
            f"the directory containing your route registrations, and that the "
            f"language was detected correctly.")

    routes = {(r["path"], r["method"].lower()): r for r in extracted}

    # Excluded paths leave the audit entirely, on both sides. A route the
    # operator has declared internal is not "missing from the spec", and the
    # spec is not wrong for staying quiet about it.
    exclude = parse_excludes(exclude)
    spec_keys = set(spec.keys())
    if exclude:
        before = len(routes) + len(spec_keys)
        routes = {k: v for k, v in routes.items() if not path_excluded(k[0], exclude)}
        spec_keys = {k for k in spec_keys if not path_excluded(k[0], exclude)}
        if not routes and not spec_keys:
            raise RouteExtractionError(
                f"exclude-paths removed all {before} endpoint(s) from the audit. "
                f"Patterns are matched against the path with --strip-prefix "
                f"already removed, so '/api/v1/*' excludes nothing while '/*' "
                f"excludes everything.")

    # The contract is what a named middleware guards. A dashboard's admin routes
    # and an integrator's API are both registered in the same codebase and are
    # told apart by which guard they sit behind, not by their path, so this is
    # the filter that survives a route being added next week.
    contract_middleware = parse_names(contract_middleware)
    if contract_middleware:
        if not any(r.get("middleware") for r in extracted):
            raise RouteExtractionError(
                "contract-middleware is set, but no route in this table records any "
                "middleware, so every route would fall outside the contract. Route "
                "middleware is extracted for TypeScript today; leave the input unset "
                "for other languages and use exclude-paths instead.")
        wanted = set(contract_middleware)
        outside = {key for key, route in routes.items()
                   if not wanted & set(route.get("middleware") or ())}
        if not set(routes) - outside:
            raise RouteExtractionError(
                f"contract-middleware {sorted(wanted)} matched no route. The names "
                f"are the identifiers as they appear in the registration, e.g. "
                f"'authenticate' in router.get('/x', authenticate, handler).")
        # Dropped from both sides by exact endpoint, so an operation the spec
        # documents and a non-contract guard protects does not then read as
        # missing from the code.
        routes = {k: v for k, v in routes.items() if k not in outside}
        spec_keys -= outside

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
    for key in sorted(spec_keys):
        if key not in routes:
            path, method = key
            findings.append(finding(
                path, method, "route_missing_from_code", "",
                f"spec documents {method.upper()} {path} but no route registers it"
                f" ({spec.source})",
                "R2"))

    # Per-operation rules. Only where code and spec both describe the endpoint.
    for key in sorted(set(routes) & spec_keys):
        path, method = key
        route = routes[key]
        operation = spec[key]
        # Prefer the definition in the file that registered the route. Keying
        # only by name collides wherever a route function and the service it
        # calls share one, and the ambiguity guard then declines both.
        facts = (table.get("handlers_by_location") or {}).get(
            f"{route['file']}::{route['handler']}") or handlers.get(route["handler"])
        findings.extend(_operation_rules(path, method, route, operation, spec,
                                         facts, structs, auth_headers))

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["path"], f["method"]))
    return findings


def _operation_rules(path, method, route, operation, spec, facts, structs, auth_headers):
    out = []
    success_codes = spec.success_codes((path, method))
    if facts and facts.get("ambiguous"):
        facts = None

    # R3/R4 - the success response body, field names then field types.
    if success_codes:
        response = operation["responses"][success_codes[0]]
        struct = structs.get(response["schema_name"]) if response["schema_name"] else None
        documented = response["properties"]

        # Statically typed languages name their response shape and it is looked
        # up by that name. Dynamically typed ones build it inline, so the
        # extractor attaches the observed shape to the handler instead. Both
        # arrive here as the same field map.
        actual = None
        if struct and comparable_struct(struct):
            actual = wire_fields(struct)
        elif facts and facts.get("response_complete") and facts.get("response_fields"):
            actual = facts["response_fields"]

        if actual and documented and not response["composed"]:

            missing = sorted(set(documented) - set(actual))
            extra = sorted(set(actual) - set(documented))
            if missing or extra:
                parts = []
                if missing:
                    parts.append(f"spec documents {', '.join(missing)} with no matching field")
                if extra:
                    parts.append(f"code returns {', '.join(extra)} undocumented")
                where = (f"{struct['file']}:{struct['line']} struct {struct['name']}"
                         if struct else f"{facts['file']}:{facts['line']} {facts['name']} response")
                out.append(finding(
                    path, method, "response_field_mismatch",
                    (missing or extra)[0], f"{where}: " + "; ".join(parts), "R3",
                    file=(struct or facts)["file"], line=(struct or facts)["line"]))

            for name in sorted(set(documented) & set(actual)):
                expected = documented[name]["type"]
                got = actual[name]["json_type"]
                if not type_conflicts(got, expected):
                    continue
                severity = "critical" if any(h in name.lower() for h in MONEY_HINTS) else "high"
                source = struct or facts
                described = (f"{struct['name']}.{actual[name]['name']} is "
                             f"{actual[name]['go_type']} (JSON {got})" if struct
                             else f"{facts['name']} returns {name} as {got}")
                out.append(finding(
                    path, method, "response_type_mismatch", name,
                    f"{source['file']}:{actual[name].get('line', source['line'])} "
                    f"{described}; spec declares {expected}",
                    "R4", severity=severity,
                    file=source["file"], line=actual[name].get("line", source["line"])))

    # Same reasoning as comparable_struct: a handler name declared in more than
    # one package cannot be attributed to this route, so every body-derived rule
    # below declines rather than guessing which declaration to cite.
    if not facts or facts.get("ambiguous"):
        # Some extractors report the declared success status on the route itself
        # (a FastAPI decorator's status_code) without emitting full handler
        # facts. That is enough to settle this one rule without a model.
        declared = route.get("status_code")
        if declared and success_codes and str(declared) not in success_codes:
            out.append(finding(
                path, method, "status_code_mismatch", success_codes[0],
                f"{route['file']}:{route['line']} declares status_code={declared} "
                f"on success; spec documents {', '.join(success_codes)}",
                "R6", file=route["file"], line=route["line"]))
        return out

    # R5/R6 - status codes the handler can actually emit.
    documented_codes = set(spec.status_codes((path, method)))
    # A framework that declares the success status on the route decorator rather
    # than in the body (FastAPI's status_code=) reports it there, not in the
    # handler facts. Either source is the code's own statement of intent.
    handler_success = facts["success_code"] or route.get("status_code") or 0
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
                f"; the documented parameter is ignored",
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
    parser.add_argument("--contract-middleware", default="",
                        help="only audit routes guarded by these middleware, newline "
                             "or comma separated, e.g. 'authenticate'")
    parser.add_argument("--exclude-paths", default="",
                        help="glob(s) to leave out of the audit, newline or comma "
                             "separated, e.g. '/auth/*,/internal/*'")
    parser.add_argument("--language", default=None, help="go or typescript; detected when omitted")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        findings = audit(args.source, args.spec, args.strip_prefix, args.language,
                         args.exclude_paths, args.contract_middleware)
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
