#!/usr/bin/env python3
"""The verification gate — component 4 of the auditor.

A claim about API behaviour is worth nothing until something executes it. This
module takes a claim, generates a Go test that asserts the *documented* contract,
runs it against the real handler through `httptest`, and reports what happened.

The rule is one line, and it is the whole project:

    a claim is CONFIRMED only if the test asserting the spec FAILS.

If the test passes, the spec is being honoured and the claim was wrong — the
finding is dropped, no matter how confident its author was. This is the only
component that can tell a real drift from a plausible sentence about one, so
every finding that reaches the report passes through here.

No model is involved. The claim can come from anywhere — a deterministic rule,
an agent, a human — and is judged the same way.

    from auditor.verify import verify_claim
    result = verify_claim(case_dir, claim, spec)

Standalone (verifies every deterministic finding for a case):

    python3 auditor/verify.py eval/cases/D01
"""

import argparse
import json
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "tools"))
from spec import load as load_spec  # noqa: E402

TEST_FILE = "contract_verify_test.go"

# Claim kinds this gate can execute. A kind absent here is reported as
# unsupported rather than waved through: silently trusting an unverifiable claim
# is the failure this component exists to prevent.
EXECUTABLE = {
    "response_field_mismatch",
    "response_type_mismatch",
    "status_code_mismatch",
    "response_header_mismatch",
    "auth_mismatch",
}


class VerificationError(RuntimeError):
    pass


# -- request synthesis ---------------------------------------------------

def sample_value(name, schema_type, spec_property=None):
    """A plausible value for a request field. Name hints matter: a BVN that is
    not 11 digits gets rejected by validation before the response shape is ever
    exercised, and the test would then be measuring the wrong thing."""
    lowered = name.lower()
    if schema_type == "integer":
        return 1
    if schema_type == "number":
        return 1
    if schema_type == "boolean":
        return True
    if schema_type == "array":
        return []
    if "bvn" in lowered:
        return "12345678901"
    if "email" in lowered:
        return "test@example.test"
    if "date" in lowered:
        return "1990-01-01"
    if "amount" in lowered or "fee" in lowered:
        return "1000"
    if "currency" in lowered:
        return "NGN"
    if "phone" in lowered:
        return "+2348000000000"
    if "reason" in lowered:
        return "duplicate"
    return f"test-{lowered or 'value'}"


def build_request(operation, path, method, include_auth=True, auth_header=None):
    """Synthesise a request that should reach the handler's success path."""
    url = path
    headers = {}
    body = None

    for param in operation["params"]:
        value = sample_value(param["name"], param["type"])
        if param["in"] == "path":
            url = url.replace("{" + param["name"] + "}", str(value))
        elif param["in"] == "header" and param["required"]:
            headers[param["name"]] = str(value)

    # Any path placeholder the spec failed to declare still has to be filled or
    # the request never reaches the handler.
    url = re.sub(r"\{([^}]+)\}", lambda m: f"test-{m.group(1)}", url)

    props = operation["request_properties"]
    if props:
        payload = {}
        for name, definition in props.items():
            if definition["required"] or len(props) <= 8:
                payload[name] = sample_value(name, definition["type"])
        body = json.dumps(payload)

    if include_auth and operation["security"] and auth_header:
        headers[auth_header] = "test-key"

    return url, headers, body


def auth_header_name(spec):
    for scheme in spec.document.get("components", {}).get("securitySchemes", {}).values():
        if scheme.get("type") == "apiKey" and scheme.get("in") == "header":
            return scheme.get("name")
        if scheme.get("type") == "http":
            return "Authorization"
    return None


# -- test generation -----------------------------------------------------

def go_literal(value):
    return json.dumps(value)


def render_test(claim, operation, handler, url, headers, body, success_code, spec):
    """Emit a Go test asserting what the specification promises.

    Written to fail loudly and specifically: the failure message is the evidence
    that ends up in the report, so it names the documented expectation and the
    observed value rather than just reporting a mismatch.
    """
    kind = claim["kind"]
    detail = claim.get("detail", "")

    header_lines = "\n".join(
        f'\treq.Header.Set({go_literal(k)}, {go_literal(v)})' for k, v in headers.items()
    )
    body_expr = f"strings.NewReader({go_literal(body)})" if body else "nil"

    setup = f'''	req := httptest.NewRequest({go_literal(claim["method"].upper())}, {go_literal(url)}, {body_expr})
{header_lines}
	req.SetPathValue("id", "test-id")
	rec := httptest.NewRecorder()
	{handler}(rec, req)
'''

    if kind == "status_code_mismatch":
        assertion = f'''	if rec.Code != {success_code} {{
		t.Fatalf("spec documents {success_code} on success; handler returned %d", rec.Code)
	}}'''

    elif kind == "response_field_mismatch":
        assertion = f'''	var payload any
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {{
		t.Fatalf("response is not JSON: %v", err)
	}}
	obj := firstObject(payload)
	if obj == nil {{
		t.Fatalf("response is not a JSON object: %s", rec.Body.String())
	}}
	if _, ok := obj[{go_literal(detail)}]; !ok {{
		t.Fatalf("spec documents field %q; response carries keys %v", {go_literal(detail)}, keys(obj))
	}}'''

    elif kind == "response_type_mismatch":
        expected = _documented_type(spec, claim, success_code, detail)
        assertion = f'''	var payload any
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {{
		t.Fatalf("response is not JSON: %v", err)
	}}
	obj := firstObject(payload)
	if obj == nil {{
		t.Fatalf("response is not a JSON object: %s", rec.Body.String())
	}}
	value, ok := obj[{go_literal(detail)}]
	if !ok {{
		t.Fatalf("field %q absent from response", {go_literal(detail)})
	}}
	if got := jsonType(value); got != {go_literal(expected)} {{
		t.Fatalf("spec declares %q as %s; response carries %s (%v)",
			{go_literal(detail)}, {go_literal(expected)}, got, value)
	}}'''

    elif kind == "response_header_mismatch":
        documented = _documented_header(spec, claim, detail)
        assertion = f'''	if rec.Header().Get({go_literal(documented)}) == "" {{
		t.Fatalf("documentation names header %q; response sent %v",
			{go_literal(documented)}, headerNames(rec.Header()))
	}}'''

    elif kind == "auth_mismatch":
        # The spec calls this endpoint public, so a request with no credentials
        # must not be rejected. Auth headers are deliberately omitted here.
        assertion = '''	if rec.Code == 401 || rec.Code == 403 {
		t.Fatalf("spec documents this endpoint as public; unauthenticated request returned %d", rec.Code)
	}'''
    else:
        raise VerificationError(f"unsupported claim kind: {kind}")

    return f'''package handlers

// Generated by auditor/verify.py to test one claim. Asserts what the
// specification promises, so a failure is the drift and a pass refutes the
// claim. Regenerated per claim; not intended to be committed.

import (
	"encoding/json"
	"net/http/httptest"
	"sort"
	"strings"
	"testing"
)

// Not every assertion template uses every import; Go treats an unused import as
// a compile error, and a test that fails to build would otherwise be
// indistinguishable from a claim that could not be confirmed.
var (
	_ = strings.NewReader
	_ = json.Unmarshal
	_ = sort.Strings
)

func keys(m map[string]any) []string {{
	out := make([]string, 0, len(m))
	for k := range m {{
		out = append(out, k)
	}}
	sort.Strings(out)
	return out
}}

func headerNames(h map[string][]string) []string {{
	out := make([]string, 0, len(h))
	for k := range h {{
		out = append(out, k)
	}}
	sort.Strings(out)
	return out
}}

// firstObject unwraps a top-level array so a list endpoint is judged by the
// shape of its elements, which is what the spec describes.
func firstObject(v any) map[string]any {{
	switch t := v.(type) {{
	case map[string]any:
		return t
	case []any:
		if len(t) > 0 {{
			if obj, ok := t[0].(map[string]any); ok {{
				return obj
			}}
		}}
	}}
	return nil
}}

func jsonType(v any) string {{
	switch v.(type) {{
	case string:
		return "string"
	case bool:
		return "boolean"
	case float64:
		return "number"
	case []any:
		return "array"
	case map[string]any:
		return "object"
	}}
	return "null"
}}

func TestContractVerify(t *testing.T) {{
{setup}
{assertion}
}}
'''


def _documented_type(spec, claim, success_code, detail):
    op = spec[(claim["path"], claim["method"])]
    response = op["responses"].get(str(success_code), {})
    return response.get("properties", {}).get(detail, {}).get("type", "string")


def _documented_header(spec, claim, detail):
    """The header the documentation actually names, which is what the test must
    assert. The claim's detail carries it when the rule found it in prose."""
    op = spec[(claim["path"], claim["method"])]
    for response in op["responses"].values():
        for header in response.get("headers", []):
            return header
    text = " ".join([op["description"], op["summary"]])
    match = re.search(r"\bX-[A-Za-z0-9-]+\b", text)
    return match.group(0) if match else detail


# -- execution -----------------------------------------------------------

def verify_claim(case_dir, claim, spec=None, keep_test=False):
    """Generate and run the test for one claim.

    Returns a dict with `verdict` in {confirmed, refuted, unsupported, error}.
    """
    case_dir = pathlib.Path(case_dir)
    api_dir = case_dir / "api" if (case_dir / "api").exists() else case_dir
    spec = spec or load_spec(case_dir / "spec" / "openapi.json")

    result = {"claim": claim, "verdict": "unsupported", "detail": "", "test": ""}

    if claim["kind"] not in EXECUTABLE:
        result["detail"] = (f"{claim['kind']} cannot be settled by executing the handler; "
                            f"it needs review rather than a test")
        return result

    key = (claim["path"], claim["method"])
    if key not in spec:
        result["verdict"] = "error"
        result["detail"] = f"{claim['method'].upper()} {claim['path']} is not in the spec"
        return result

    operation = spec[key]

    # A claim about a field must name a field the spec actually documents.
    # Without this, a hallucinated field name generates a test asserting that
    # an invented key is present, the assertion fails because the key was never
    # promised, and the gate confirms a finding that describes nothing. The gate
    # would then be laundering hallucinations instead of catching them.
    if claim["kind"] in ("response_field_mismatch", "response_type_mismatch"):
        codes = spec.success_codes(key)
        documented = operation["responses"].get(codes[0], {}).get("properties", {}) if codes else {}
        if claim.get("detail") not in documented:
            result["verdict"] = "error"
            result["detail"] = (f"claim names field {claim.get('detail')!r}, which this "
                                f"operation's response schema does not document")
            return result

    # Resolve the handler from the route table so the test calls the same
    # function the route registers, not one matched by name similarity.
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "tools"))
    from routes import extract  # noqa: E402
    table = extract(api_dir, strip_prefix="/v1")
    route = next((r for r in table["routes"]
                  if r["path"] == claim["path"] and r["method"].lower() == claim["method"]), None)
    if route is None or not route["handler"]:
        result["verdict"] = "error"
        result["detail"] = "no handler registered for this route"
        return result

    success_codes = spec.success_codes(key)
    success_code = success_codes[0] if success_codes else "200"

    include_auth = claim["kind"] != "auth_mismatch"
    url, headers, body = build_request(operation, claim["path"], claim["method"],
                                       include_auth=include_auth,
                                       auth_header=auth_header_name(spec))

    source = render_test(claim, operation, route["handler"], url, headers, body,
                         success_code, spec)
    result["test"] = source

    package_dir = api_dir / pathlib.Path(route["file"]).parent
    test_path = package_dir / TEST_FILE
    test_path.write_text(source)

    try:
        completed = subprocess.run(
            ["go", "test", "-run", "TestContractVerify", "-count=1", "./..."],
            cwd=api_dir, capture_output=True, text=True, timeout=120,
        )
        output = (completed.stdout + completed.stderr).strip()
        result["output"] = output

        if "build failed" in output or "cannot use" in output or "undefined:" in output:
            result["verdict"] = "error"
            result["detail"] = "generated test did not compile"
        elif completed.returncode != 0:
            result["verdict"] = "confirmed"
            result["detail"] = _failure_message(output)
        else:
            result["verdict"] = "refuted"
            result["detail"] = "the handler honours the spec; claim dropped"
    except subprocess.TimeoutExpired:
        result["verdict"] = "error"
        result["detail"] = "test timed out"
    finally:
        if not keep_test and test_path.exists():
            test_path.unlink()

    return result


def _failure_message(output):
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("contract_verify_test.go:"):
            return stripped.split(":", 2)[-1].strip()
    return output.splitlines()[0] if output else "test failed"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("case", help="path to an evaluation case directory")
    parser.add_argument("--findings", help="findings JSON (default: the case's deterministic run)")
    parser.add_argument("--keep-test", action="store_true", help="leave the generated test in place")
    args = parser.parse_args()

    case_dir = pathlib.Path(args.case).resolve()
    if args.findings:
        claims = json.loads(pathlib.Path(args.findings).read_text())["findings"]
    else:
        root = pathlib.Path(__file__).resolve().parents[1]
        run = root / "reports" / "runs" / "deterministic" / f"{case_dir.name}.json"
        if not run.exists():
            sys.exit("no findings to verify. Run: make deterministic")
        claims = json.loads(run.read_text())["findings"]

    if not claims:
        print(f"{case_dir.name}: no claims to verify")
        return

    spec = load_spec(case_dir / "spec" / "openapi.json")
    print(f"{case_dir.name}: verifying {len(claims)} claim(s)\n")
    for claim in claims:
        outcome = verify_claim(case_dir, claim, spec, keep_test=args.keep_test)
        mark = {"confirmed": "CONFIRMED", "refuted": "REFUTED  ",
                "unsupported": "SKIPPED  ", "error": "ERROR    "}[outcome["verdict"]]
        print(f"  {mark}  {claim['method'].upper():<6} {claim['path']:<22} {claim['kind']}")
        if outcome["detail"]:
            print(f"             {outcome['detail']}")


if __name__ == "__main__":
    main()
