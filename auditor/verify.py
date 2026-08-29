#!/usr/bin/env python3
"""The verification gate: component 4 of the auditor.

A claim about API behaviour is worth nothing until something executes it. This
module takes a claim, generates a Go test that asserts the *documented* contract,
runs it against the real handler through `httptest`, and reports what happened.

The rule is one line, and it is the whole project:

    a claim is CONFIRMED only if the test asserting the spec FAILS.

If the test passes, the spec is being honoured and the claim was wrong; the
finding is dropped, no matter how confident its author was. This is the only
component that can tell a real drift from a plausible sentence about one, so
every finding that reaches the report passes through here.

No model is involved. The claim can come from anywhere (a deterministic rule,
an agent, a human) and is judged the same way.

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
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from spec import load as load_spec  # noqa: E402
import languages  # noqa: E402
import verify_ts  # noqa: E402
import verify_py  # noqa: E402
import verify_php  # noqa: E402

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
    # The three judgment kinds. They need a model to notice, but not to confirm:
    # each one predicts a concrete request the handler will mishandle, so each
    # can be executed like any other claim. Leaving them unverified would have
    # meant the only findings reaching the report on trust were the ones no
    # parser could check - exactly the wrong place to relax the rule.
    "request_required_mismatch",
    "default_value_mismatch",
    "validation_mismatch",
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


def constrained_value(name, definition):
    """A value that satisfies the documented constraints exactly.

    Used to probe a validation claim: if the spec says a field is 11 characters,
    send exactly 11. A handler honouring the contract accepts it; one enforcing a
    different bound rejects it, and that rejection is the evidence.
    """
    if not definition:
        return sample_value(name, "string")

    if definition.get("enum"):
        return definition["enum"][0]

    schema_type = definition.get("type", "string")
    if schema_type in ("integer", "number"):
        if definition.get("minimum") is not None:
            return definition["minimum"]
        if definition.get("maximum") is not None:
            return definition["maximum"]
        return sample_value(name, schema_type)

    if schema_type == "string":
        minimum = definition.get("minLength")
        maximum = definition.get("maxLength")
        target = minimum if minimum is not None else maximum
        if target:
            base = sample_value(name, "string")
            digits = "0123456789"
            # Digit-shaped fields (BVN, account numbers) are usually validated
            # for content as well as length, so pad with digits when the sample
            # value is already numeric.
            filler = digits if str(base).isdigit() else "x"
            return (str(base) * (target // len(str(base)) + 1))[:target] if len(str(base)) >= target \
                else (str(base) + filler * target)[:target]
    return sample_value(name, schema_type)


def build_request(operation, path, method, include_auth=True, auth_header=None,
                  omit_field=None, constrain_field=None, omit_param=None):
    """Synthesise a request that should reach the handler's success path.

    `omit_field` leaves one body field out - used to probe a claim that the
    handler requires something the spec calls optional. `constrain_field` sets
    one field to a value satisfying its documented constraint. `omit_param`
    leaves a query parameter off so the handler's default is observable.
    """
    url = path
    headers = {}
    body = None

    for param in operation["params"]:
        value = sample_value(param["name"], param["type"])
        if param["in"] == "path":
            url = url.replace("{" + param["name"] + "}", str(value))
        elif param["in"] == "header" and param["required"] and param["name"] != omit_field:
            headers[param["name"]] = str(value)

    # Any path placeholder the spec failed to declare still has to be filled or
    # the request never reaches the handler.
    url = re.sub(r"\{([^}]+)\}", lambda m: f"test-{m.group(1)}", url)

    props = operation["request_properties"]
    if props:
        payload = {}
        for name, definition in props.items():
            if name == omit_field:
                continue
            if not (definition["required"] or len(props) <= 8):
                continue
            if name == constrain_field:
                payload[name] = constrained_value(name, definition)
            else:
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

    elif kind == "request_required_mismatch":
        # The spec says this field is optional. Send a request that omits it but
        # carries everything the spec does require. A 4xx proves the handler
        # demands more than the contract promises.
        assertion = f'''	if rec.Code >= 400 && rec.Code < 500 {{
		t.Fatalf("spec does not require %q, but omitting it returned %d: %s",
			{go_literal(detail)}, rec.Code, strings.TrimSpace(rec.Body.String()))
	}}'''

    elif kind == "default_value_mismatch":
        documented = _documented_default(spec, claim, detail)
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
		t.Skipf("response does not echo %q; default not observable here", {go_literal(detail)})
	}}
	if got := toNumber(value); got != {documented} {{
		t.Fatalf("spec documents a default of %v for %q; handler applied %v",
			{documented}, {go_literal(detail)}, value)
	}}'''

    elif kind == "validation_mismatch":
        # Send a value that satisfies the documented constraint exactly. A
        # handler honouring the spec accepts it; one enforcing a different bound
        # rejects it.
        assertion = f'''	if rec.Code >= 400 && rec.Code < 500 {{
		t.Fatalf("value for %q satisfies the documented constraint, but the handler returned %d: %s",
			{go_literal(detail)}, rec.Code, strings.TrimSpace(rec.Body.String()))
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
	_ = strings.TrimSpace
	_ = toNumber
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

func toNumber(v any) float64 {{
	if f, ok := v.(float64); ok {{
		return f
	}}
	return -1
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


def _resolve_detail(claim, operation, spec, key):
    """Map a claim's `detail` onto a name the specification declares.

    Returns the original string when nothing matches, so an unresolvable detail
    still fails the documented-field check rather than silently binding to the
    wrong field.
    """
    detail = (claim.get("detail") or "").strip()
    if not detail:
        return detail

    names = {p["name"] for p in operation["params"]}
    names |= set(operation["request_properties"])
    for response in operation["responses"].values():
        names |= set(response.get("properties", {}))

    if detail in names:
        return detail

    # A quoted or backticked name is the strongest signal: the model is naming
    # the field rather than describing it.
    quoted = re.findall(r"[\"'`]([A-Za-z_][A-Za-z0-9_]*)[\"'`]", detail)
    for candidate in quoted:
        if candidate in names:
            return candidate

    # Then whole tokens. Substring matching is wrong here and was actively
    # harmful: a model writing "carries feeAmount but the spec documents fee"
    # resolved to `amount`, because "amount" is a substring of "feeAmount". The
    # probe then asserted a field that was present, and the gate refuted a
    # finding that was true. Split into identifier tokens and match whole ones.
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", detail)
    token_set = set(tokens)
    for name in sorted(names, key=len, reverse=True):
        if name in token_set:
            return name

    # Last resort: a word-boundary match, so a name embedded in prose is still
    # found, but a name embedded inside a longer identifier is not.
    for name in sorted(names, key=len, reverse=True):
        if name and re.search(rf"\b{re.escape(name)}\b", detail):
            return name
    return detail


def _positional_args(operation, claim, path_params, headers, request_body):
    """Positional arguments for a controller method.

    PHP has no keyword arguments, so the order has to be inferred: a request body
    first where the operation declares one, then path parameters in the order the
    spec lists them. Where the guess is wrong the call raises rather than
    silently testing something else, and the gate reports an error instead of a
    verdict - which is the safe direction.
    """
    args = []
    parsed_body = json.loads(request_body) if request_body else None
    if parsed_body is not None:
        args.append(parsed_body)
    for param in operation["params"]:
        if param["in"] == "path" and param["name"] in path_params:
            args.append(path_params[param["name"]])
    if parsed_body is not None and headers:
        args.append(headers)
    return args


def _python_kwargs(route, operation, claim, path_params, headers, request_body):
    """Arguments for calling a Python handler directly.

    Built from the handler's real parameter names, matched against path
    parameters, query parameters and the request body. A name the route does not
    declare is left out: passing an argument the function does not accept raises
    a TypeError that would read as a failed verification rather than a bad probe.
    """
    kwargs = {}
    declared = route.get("args") or []
    parsed_body = json.loads(request_body) if request_body else None

    for name in declared:
        if name in path_params:
            kwargs[name] = path_params[name]
            continue
        header_match = next((v for k, v in headers.items()
                             if k.lower().replace("-", "_") == name.lower()), None)
        if header_match is not None:
            kwargs[name] = header_match
            continue
        if name in ("body", "payload", "data", "request_body") and parsed_body is not None:
            kwargs[name] = parsed_body
            continue
        param = next((p for p in operation["params"] if p["name"] == name), None)
        if param is not None:
            # A default-value probe must omit the parameter so the handler's own
            # default is what the response reflects.
            if claim["kind"] == "default_value_mismatch" and name == claim.get("detail"):
                continue
            if param.get("default") is None:
                kwargs[name] = sample_value(name, param.get("type", "string"))
    return kwargs


def _documented_properties(spec, key, success_code):
    op = spec[key]
    return op["responses"].get(str(success_code), {}).get("properties", {})


def _raw_default(operation, detail):
    for param in operation["params"]:
        if param["name"] == detail and param.get("default") is not None:
            return param["default"]
    return None


def _documented_default(spec, claim, detail):
    """The default the spec documents for a parameter, as a Go literal."""
    op = spec[(claim["path"], claim["method"])]
    for param in op["params"]:
        if param["name"] == detail and param.get("default") is not None:
            return json.dumps(param["default"])
    return "0"


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

def verify_claim(case_dir, claim, spec=None, keep_test=False, language=None,
                 strip_prefix="/v1"):
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

    # Resolve a prose `detail` down to the identifier it refers to. A
    # deterministic rule emits a bare name; a model writes a phrase around it
    # ("perPage query parameter default value"). Matching that phrase against the
    # names the spec declares keeps both usable without requiring the model to be
    # perfectly terse - and an unresolved detail was silently losing true claims.
    claim = dict(claim, detail=_resolve_detail(claim, operation, spec, key))

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
    adapter = languages.get(language) if language else languages.detect(api_dir)
    if adapter is None:
        result["verdict"] = "error"
        result["detail"] = f"could not identify the language of {api_dir}"
        return result
    table = adapter.extract(api_dir, strip_prefix=strip_prefix)
    route = next((r for r in table["routes"]
                  if r["path"] == claim["path"] and r["method"].lower() == claim["method"]), None)
    if route is None or not route["handler"]:
        result["verdict"] = "error"
        result["detail"] = "no handler registered for this route"
        return result

    success_codes = spec.success_codes(key)
    success_code = success_codes[0] if success_codes else "200"

    kind = claim["kind"]
    detail = claim.get("detail") or ""
    path_params = {p["name"]: sample_value(p["name"], p["type"])
                   for p in operation["params"] if p["in"] == "path"}
    query = {p["name"]: p.get("default")
             for p in operation["params"]
             if p["in"] == "query" and p["name"] != (detail if kind == "default_value_mismatch" else None)
             and p.get("default") is not None}
    query = {}   # omitted by default; a probe that needs one sets it explicitly

    url, headers, body = build_request(
        operation, claim["path"], claim["method"],
        include_auth=(kind != "auth_mismatch"),
        auth_header=auth_header_name(spec),
        # Each judgment kind predicts a specific request the handler mishandles;
        # the probe has to be that request, not a generic valid one.
        omit_field=detail if kind == "request_required_mismatch" else None,
        constrain_field=detail if kind == "validation_mismatch" else None,
        omit_param=detail if kind == "default_value_mismatch" else None,
    )

    if adapter.NAME == "php":
        module = adapter._controller_file(api_dir, route)
        if not module:
            result["verdict"] = "error"
            result["detail"] = f"could not locate the controller defining {route['handler']!r}"
            return result
        klass = adapter.controller_class(api_dir, module)
        documented = dict(_documented_properties(spec, key, success_code))
        documented["__default__"] = _raw_default(operation, claim.get("detail"))
        args = _positional_args(operation, claim, path_params, headers, body)
        source = verify_php.render_test(
            claim, operation, module, klass, route["handler"], args,
            success_code, documented, declared_status=route.get("status_code"))
    elif adapter.NAME == "python":
        documented = dict(_documented_properties(spec, key, success_code))
        documented["__default__"] = _raw_default(operation, claim.get("detail"))
        kwargs = _python_kwargs(route, operation, claim, path_params, headers, body)
        source = verify_py.render_test(
            claim, operation, route["file"], route["handler"], kwargs,
            success_code, documented, declared_status=route.get("status_code"))
    elif adapter.NAME == "typescript":
        module = adapter.handler_module(api_dir, route, table)
        if not module:
            result["verdict"] = "error"
            result["detail"] = f"could not locate the module defining {route['handler']}"
            return result
        # The test is written beside the route file, so the import is relative to
        # that directory.
        test_dir = pathlib.Path(route["file"]).parent
        rel_module = str(pathlib.Path(module).relative_to(test_dir)) \
            if str(test_dir) != "." and str(module).startswith(str(test_dir)) \
            else pathlib.Path(module).name
        documented = dict(_documented_properties(spec, key, success_code))
        documented["__default__"] = _raw_default(operation, claim.get("detail"))
        source = verify_ts.render_test(
            claim, operation, rel_module, route["handler"],
            {"method": claim["method"].upper(), "params": path_params,
             "query": query, "headers": headers, "body": json.loads(body) if body else None},
            success_code, documented)
    else:
        source = render_test(claim, operation, route["handler"], url, headers, body,
                             success_code, spec)
    result["test"] = source

    test_path = adapter.test_path(api_dir, route)
    test_path.write_text(source)
    command = adapter.test_command(api_dir)
    cwd = test_path.parent if adapter.NAME == "typescript" else api_dir

    try:
        completed = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, timeout=180,
        )
        output = (completed.stdout + completed.stderr).strip()
        result["output"] = output

        if adapter.build_failed(output):
            result["verdict"] = "error"
            result["detail"] = "generated test did not compile"
        elif adapter.skipped(output):
            # `go test` exits 0 on a skip, which an exit-code check reads as a
            # passing test and therefore as a refuted claim. A skip means the
            # probe could not be built - unknown, not false. Conflating the two
            # silently discards true findings, which is how D09 was lost.
            result["verdict"] = "unsupported"
            result["detail"] = _skip_reason(output) or "probe could not be constructed"
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


def _skip_reason(output):
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("contract_verify_test.go:") and "not observable" in stripped:
            return stripped.split(":", 2)[-1].strip()
    return ""


def _failure_message(output):
    """The assertion message, which becomes the evidence in the report.

    Each runner buries it differently: Go prefixes it with the test file and
    line, node puts it after an AssertionError banner. Reporting the runner's
    summary line instead would put "test failed" in front of a reviewer where
    the observed behaviour should be.
    """
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("contract_verify_test.go:"):
            return stripped.split(":", 2)[-1].strip()
        if "AssertionError" in stripped and ":" in stripped:
            message = stripped.split(":", 2)[-1].strip()
            if message:
                return message
    for line in output.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("\u2716", "\u2714", "\u2139", "#", "at ")):
            return stripped
    return "test failed"


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
