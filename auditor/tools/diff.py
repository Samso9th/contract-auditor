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
import re
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
    "auth_guard_missing": "critical",
    "auth_guard_undocumented": "high",
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
    declaration rather than hardcoded, so this generalises past the fixture.

    Both spellings, because both are in circulation: OpenAPI 3 declares them
    under components.securitySchemes, Swagger 2.0 at the top level as
    securityDefinitions. Reading only the newer one left every 2.0 document
    looking as though it promised integrators no credential at all - which is
    what `swaggo` emits, and so most annotated Go - and with no credential named
    there is nothing to identify the contract guard by.
    """
    schemes = dict(spec.document.get("securityDefinitions") or {})
    schemes.update((spec.document.get("components") or {}).get("securitySchemes") or {})
    names = set()
    for scheme in schemes.values():
        if not isinstance(scheme, dict):
            continue
        if scheme.get("type") == "apiKey" and scheme.get("in") == "header":
            names.add(scheme.get("name", ""))
        # `http` is OpenAPI 3; 2.0 spells the same thing `basic`. Either way the
        # credential travels in Authorization.
        if scheme.get("type") in ("http", "basic"):
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


# A middleware has to be exported to be imported by the file registering the
# route, and declared at the top level to be exported. Both halves matter:
# without the export test, `const payload = jwt.verify(...)` inside a guard reads
# as a middleware named `payload`.
GUARD_DEFINITION = (
    r"^export\s+(?:default\s+)?(?:async\s+)?(?:function|const|let|var|class)\s+{name}\b"
    r"|^export\s*\{{[^}}]*\b{name}\b[^}}]*\}}"
    r"|^(?:module\.)?exports\.{name}\s*="
    # `module.exports = { validApiKey }`, which is how CommonJS actually
    # exports a middleware. Without this line the dominant export style in
    # JavaScript matched nothing, so every guard in such a project was invisible
    # and its whole dashboard read as part of the published API.
    r"|^(?:module\.)?exports\s*=\s*\{{[^}}]*\b{name}\b"
    r"|^def\s+{name}\b"
    r"|^func\s+(?:\([^)]*\)\s*)?{name}\b"
)

GUARD_SUFFIXES = (".ts", ".js", ".mjs", ".tsx", ".go", ".py", ".php")
GUARD_SKIP = {".git", "node_modules", "vendor", "dist", "build", "__pycache__",
              ".venv", "venv", ".next", "target", "coverage"}


# Where a middleware's own definition starts. Narrower than GUARD_DEFINITION,
# which also accepts an export statement: an export tells you a name leaves the
# file, not what the function does.
GUARD_BODY = (
    r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+{name}\b"
    r"|^(?:export\s+)?(?:const|let|var)\s+{name}\s*="
    r"|^def\s+{name}\b"
    r"|^func\s+(?:\([^)]*\)\s*)?{name}\b"
)

# The start of the next top-level definition, which is where the previous one's
# body ends closely enough for a text scan.
NEXT_DEFINITION = re.compile(
    r"^(?:export\s|(?:module\.)?exports\b|func\s|def\s|class\s|"
    r"(?:async\s+)?function\s|const\s|let\s|var\s)", re.M)


def strip_factory(name):
    """`validate(schema)` and `validate` are the same guard under two spellings;
    the route table keeps the parentheses so a reader can tell them apart."""
    return name[:-2] if name.endswith("()") else name


def _definition_body(text, start):
    """From a definition to the start of the next one at the top level."""
    following = NEXT_DEFINITION.search(text, start)
    return text[start:following.start()] if following else text[start:]


def guards_reading(root, names, headers):
    """Of `names`, those that authenticate: the ones whose own definition reads
    one of `headers`.

    The spec is the authority on what a credential is: an apiKey scheme names
    the header, http auth means Authorization. A middleware that reads one of
    them is an authentication guard, whatever it happens to be called.

    The test is the function's own body, not its file. A file-wide match was the
    first version and it does not discriminate: a router's middleware are
    usually declared together, so one auth guard among them makes the whole file
    mention Authorization and every logging, tracing and content-type middleware
    beside it qualifies. On a real repository that produced four "contract
    guards", none of which authenticated anything.

    The file-wide test survives as a fallback, for the languages where the
    definition and the export are far apart and only the export can be found.
    """
    names = sorted({strip_factory(n) for n in names})
    names = [n for n in names if n.isidentifier()]
    if not names or not headers:
        return set()

    sources = []
    for path in pathlib.Path(root).rglob("*"):
        if path.is_dir() or path.suffix.lower() not in GUARD_SUFFIXES:
            continue
        if any(part in GUARD_SKIP for part in path.parts):
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        if any(h.lower() in text.lower() for h in headers):
            sources.append(text)

    def reads(text, start):
        body = _definition_body(text, start)
        return any(h.lower() in body.lower() for h in headers)

    found, exported = set(), set()
    for name in names:
        body_pattern = re.compile(GUARD_BODY.format(name=re.escape(name)), re.M)
        any_pattern = re.compile(GUARD_DEFINITION.format(name=re.escape(name)), re.M)
        for text in sources:
            match = body_pattern.search(text)
            if match and reads(text, match.end()):
                found.add(name)
                break
            if any_pattern.search(text):
                exported.add(name)
    return found or exported


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


def _canonical(path):
    """The path with a trailing slash removed.

    Express, Flask, chi and Laravel all answer /tags and /tags/ with the same
    handler. OpenAPI treats them as two paths. So a router written without them
    and a document written with them describe one API and share not a single
    key, which reads as every endpoint being both undocumented and
    unimplemented.
    """
    return path.rstrip("/") or "/"


def _shape(path):
    """The path with parameter names erased: /vault/{filename} -> /vault/{}.

    Nothing requires the name in the code to be the name in the document, and
    an unnamed splat has no name to agree on: /vault/* is registered by Express
    and documented as /vault/{filename}. The segments and their order are what
    identify an endpoint; what the parameter is called is not.
    """
    return re.sub(r"\{[^}]*\}", "{}", _canonical(path))


def _unique_index(keys, normalise):
    """Normalised key -> the single key that has it.

    A normalised key claimed by two keys is dropped rather than resolved. That
    is the whole safety property of looser matching: it may pair two spellings
    of one endpoint, and must never merge two endpoints that are genuinely
    different. A spec really can document /vault/{filename} and
    /vault/{pathToDirectory}/ as separate operations, and this is what stops
    them collapsing into one.
    """
    index, clashed = {}, set()
    for path, method in keys:
        norm = (normalise(path), method)
        if norm in index:
            clashed.add(norm)
        index[norm] = (path, method)
    return {k: v for k, v in index.items() if k not in clashed}


WILDCARD = "{wildcard}"

# A registration that answers every verb: chi and gorilla spell it ANY, Express
# spells it ALL. Only one of the two was skipped, so a TypeScript project using
# app.all() had it reported as an undocumented endpoint that no specification
# could ever document, since there is no such method to write down.
METHODLESS = {"ANY", "ALL"}


def _splat_prefix(path):
    """The literal prefix of a route registered with a trailing splat.

    Express answers /vault/, /vault/note.md and /vault/dir/note.md from one
    /vault/* registration, and a specification documents those as three
    operations. One route implementing several documented operations is not
    drift. Counting it as one undocumented route plus three unimplemented ones
    is, and it was four of one repository's findings.

    A splat at the root is a fallback handler rather than an endpoint, so it
    covers nothing: letting /* absorb the whole specification would report a
    catch-all as a complete implementation of it.
    """
    canonical = _canonical(path)
    if not canonical.endswith("/" + WILDCARD):
        return None
    prefix = _canonical(canonical[: -len("/" + WILDCARD)])
    return None if prefix == "/" else prefix


def reconcile(route_keys, spec_keys):
    """Pair each route with the operation or operations it implements.

    Returns the pairs, the routes that matched nothing, and the operations that
    matched nothing.

    The two sides agree about an endpoint far more often than they agree on how
    to write it down, and comparing them as strings counts every difference of
    spelling twice: once as a route the spec forgot, once as an operation the
    code never implemented. On the first real repository this was tried against
    that was 21 of 23 endpoints, every one of them both implemented and
    documented.

    Four passes, each seeing only what the last left over, so a looser rule
    never overrules a stricter one:

        exact              /tags        == /tags
        trailing slash     /tags        == /tags/
        parameter names    /v/{file}    == /v/{name}
        splat coverage     /v/*         covers /v/ and /v/{name}

    The first three are one-to-one and refuse anything ambiguous. Only the
    fourth is one-to-many, because only a splat genuinely serves many paths.
    """
    pairs = []
    routes_left, spec_left = set(route_keys), set(spec_keys)

    def take(route_key, spec_key):
        pairs.append((route_key, spec_key))
        routes_left.discard(route_key)
        spec_left.discard(spec_key)

    for key in sorted(routes_left & spec_left):
        take(key, key)

    for normalise in (_canonical, _shape):
        by_route = _unique_index(routes_left, normalise)
        by_spec = _unique_index(spec_left, normalise)
        for norm, route_key in sorted(by_route.items()):
            spec_key = by_spec.get(norm)
            if spec_key is not None:
                take(route_key, spec_key)

    # Method-less registrations go last. app.all('/vault/*') and app.get(
    # '/vault/*') can both claim GET /vault/{file}, and the one that named the
    # verb is the better claim; letting the catch-all go first leaves the
    # specific route with nothing to pair with and reports it as undocumented.
    for route_key in sorted(routes_left,
                            key=lambda k: (k[1].upper() in METHODLESS, k)):
        path, method = route_key
        prefix = _splat_prefix(path)
        methodless = method.upper() in METHODLESS
        if prefix is None and not methodless:
            continue

        def serves(spec_key):
            spec_path, spec_method = spec_key
            # app.all('/mcp') answers GET and POST there, so it implements both
            # of the operations the spec documents at that path.
            if not methodless and spec_method != method:
                return False
            if prefix is not None:
                canonical = _canonical(spec_path)
                return canonical == prefix or canonical.startswith(prefix + "/")
            return _shape(spec_path) == _shape(path)

        for spec_key in sorted(key for key in spec_left if serves(key)):
            take(route_key, spec_key)

    # A parameter in the code covering a literal in the document:
    #
    #   code  PUT /resources/{id}/{vote_direction}
    #   spec  PUT /resources/{id}/upvote
    #         PUT /resources/{id}/downvote
    #
    # One handler serving several documented paths, the same relation the splat
    # pass covers and for the same reason. Reporting it as one undocumented
    # route plus two unimplemented operations describes an API that works as
    # three faults.
    #
    # Last, so a route matching a literal exactly has already claimed it: only
    # what nothing more specific wanted is offered to the general case.
    for route_key in sorted(routes_left):
        path, method = route_key
        segments = _canonical(path).split("/")
        matched = []
        for spec_key in sorted(spec_left):
            spec_path, spec_method = spec_key
            if spec_method != method:
                continue
            spec_segments = _canonical(spec_path).split("/")
            if len(spec_segments) != len(segments):
                continue
            if any(ours != theirs and not _is_parameter(ours)
                   for ours, theirs in zip(segments, spec_segments)):
                continue
            matched.append(spec_key)
        for spec_key in matched:
            take(route_key, spec_key)

    return pairs, routes_left, spec_left


def _is_parameter(segment):
    return segment.startswith("{") and segment.endswith("}")


# Paths that are undocumented on purpose in nearly every project: the docs UI
# itself, the health check a load balancer calls, the files a browser asks for
# without being told to.
INCIDENTAL_PATHS = (
    "/robots.txt", "/favicon.ico", "/manifest.json", "/sitemap.xml",
    "/health", "/healthz", "/readyz", "/livez", "/ping", "/status", "/metrics",
    "/docs", "/redoc", "/swagger", "/swagger-ui", "/openapi.json",
    "/openapi.yaml", "/openapi.yml", "/swagger.json", "/swagger.yaml",
    "/.well-known",
)


def undocumented_severity(route, contract_middleware):
    """How much an undocumented route matters, which is not the same for all of
    them.

    With contract-middleware set, every route still in the table is inside the
    published contract by construction, so one the spec omits is a real hole in
    that contract: high.

    Without it the table is the whole codebase, most of which was never meant to
    be published, and there is no signal here saying which is which. Calling all
    of it high is what turned a first run on a real repository into 277 findings
    of identical weight, the route serving the documentation among them. The
    finding is still true and still reported; what changes is that the reader is
    no longer told a favicon and an unpublished payments endpoint are the same
    size of problem.
    """
    if contract_middleware:
        return "high"
    path = _canonical((route.get("path") or "").lower())
    if path == "/" or any(path == p or path.startswith(p + "/")
                          for p in INCIDENTAL_PATHS):
        return "low"
    return "medium"


def assertion_coverage(spec, spec_keys, paired):
    """What the specification actually asserts, and how much of it was checked.

    No findings in a category means one of two opposite things: the code agreed
    with the specification, or the specification said nothing to disagree with.
    A report that renders them identically is quietly telling the reader the
    more flattering one.

    This is not a rare edge. On the first real repository audited, not one of 63
    documented success responses carried a schema. Every response rule was
    silent and the report read as a clean bill of health, when the truthful
    summary was that the document makes almost no checkable promise about what
    comes back.
    """
    matched = {spec_key for _, spec_key in paired}
    counts = {"operations": len(spec_keys), "operations_matched": len(matched),
              "success_responses": 0, "success_responses_with_schema": 0,
              "error_responses": 0, "error_responses_with_schema": 0,
              "parameters": 0}
    for key in spec_keys:
        operation = spec[key]
        counts["parameters"] += len(operation.get("params") or [])
        for code, response in (operation.get("responses") or {}).items():
            described = bool(response.get("properties")
                             or response.get("schema_name"))
            bucket = "success" if str(code).startswith("2") else "error"
            counts[f"{bucket}_responses"] += 1
            counts[f"{bucket}_responses_with_schema"] += int(described)
    return counts


def audit(source_dir, spec_path, strip_prefix="", language=None, exclude=(),
          contract_middleware=(), coverage=None):
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
        # strip_factory on both sides: a guard built by a factory keeps its
        # parentheses in the route table - RequireAuth(logger) is recorded as
        # RequireAuth() - while the input names it without them.
        wanted = {strip_factory(n) for n in contract_middleware}
        # An endpoint the spec documents is inside the contract whatever guards
        # it, because the spec is the contract. Dropping it for carrying the
        # wrong guard would silently delete the two findings this input exists
        # to make possible: a documented endpoint registered with no guard at
        # all, and one guarded by something that answers a different credential.
        documented = {route_key for route_key, _ in
                      reconcile(set(routes), spec_keys)[0]}
        outside = {key for key, route in routes.items()
                   if not wanted & {strip_factory(m)
                                    for m in (route.get("middleware") or ())}
                   and key not in documented}
        if not set(routes) - outside:
            raise RouteExtractionError(
                f"contract-middleware {sorted(wanted)} matched no route. The names "
                f"are the identifiers as they appear in the registration, e.g. "
                f"'authenticate' in router.get('/x', authenticate, handler).")
        # Only the routes leave. Everything the spec documents stays on both
        # sides, so nothing it promises can go unaudited.
        routes = {k: v for k, v in routes.items() if k not in outside}

    auth_headers = auth_header_names(spec)

    findings = []

    # Which route implements which operation, before any rule runs. The two
    # sides spell the same endpoint differently far more often than they
    # disagree about it, so this is settled once rather than by each rule.
    paired, unmatched_routes, unmatched_spec = reconcile(set(routes), spec_keys)

    # R1 - registered in code, absent from the spec.
    for key in sorted(unmatched_routes):
        route = routes[key]
        if route["method"] in METHODLESS:
            continue  # a method-less pattern cannot be matched to one operation
        findings.append(finding(
            route["path"], route["method"], "route_missing_from_spec", "",
            f"{route['file']}:{route['line']} registers {route['method']} {route['path']}"
            f" (handler {route['handler']}), which the spec does not document",
            "R1", severity=undocumented_severity(route, contract_middleware)))

    # R2 - documented in the spec, not registered in code.
    for key in sorted(unmatched_spec):
        path, method = key
        findings.append(finding(
            path, method, "route_missing_from_code", "",
            f"spec documents {method.upper()} {path} but no route registers it"
            f" ({spec.source})",
            "R2"))

    # Per-operation rules. Only where code and spec both describe the endpoint.
    for route_key, spec_key in sorted(paired):
        path, method = route_key
        route = routes[route_key]
        operation = spec[spec_key]
        # Prefer the definition in the file that registered the route. Keying
        # only by name collides wherever a route function and the service it
        # calls share one, and the ambiguity guard then declines both.
        facts = (table.get("handlers_by_location") or {}).get(
            f"{route['file']}::{route['handler']}") or handlers.get(route["handler"])
        findings.extend(_operation_rules(path, method, route, operation, spec,
                                         facts, structs, auth_headers,
                                         spec_key=spec_key))

    # R9/R10 - the route's guard against the security the spec declares.
    #
    # Only where the language records route middleware. R8 below reads the
    # handler, and notes that a handler which does not read a header proves
    # nothing, because auth is usually middleware. This is that missing half.
    #
    # Reported per file, and only for files that guard something: a project
    # applying auth once at app level registers no middleware here, and calling
    # every one of its routes unguarded would be a page of false alarms rather
    # than a finding.
    if any(r.get("middleware") for r in extracted):
        every = {m for r in extracted for m in (r.get("middleware") or [])}
        guard_names = guards_reading(source_dir, every, auth_headers)
        guarded_files = {r["file"] for r in extracted
                         if guard_names & set(r.get("middleware") or ())}

        # Once per route, not once per pair. R9 and R10 are statements about
        # the registration - what guards it - and a route serving several
        # documented operations has one registration, not several.
        seen_routes = set()
        for route_key, spec_key in sorted(paired):
            if route_key in seen_routes:
                continue
            seen_routes.add(route_key)
            path, method = route_key
            route, operation = routes[route_key], spec[spec_key]
            on_route = guard_names & set(route.get("middleware") or ())

            if operation["security"] and not on_route and route["file"] in guarded_files:
                findings.append(finding(
                    path, method, "auth_guard_missing", "",
                    f"{route['file']}:{route['line']} registers {method.upper()} {path} "
                    f"with no authentication middleware, while the spec declares "
                    f"{', '.join(operation['security'])} for it. Other routes in the "
                    f"same file are guarded by "
                    f"{', '.join(sorted(guard_names)) or 'a guard'}",
                    "R9", file=route["file"], line=route["line"]))

            if operation["security"] == [] and on_route:
                findings.append(finding(
                    path, method, "auth_guard_undocumented", "",
                    f"{route['file']}:{route['line']} guards {method.upper()} {path} "
                    f"with {', '.join(sorted(on_route))}, while the spec documents it "
                    f"as needing no authentication. An integrator following the spec "
                    f"is answered 401",
                    "R10", file=route["file"], line=route["line"]))

    if coverage is not None:
        coverage.update(assertion_coverage(spec, spec_keys, paired))
        coverage["routes"] = len(routes)

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["path"], f["method"]))
    return findings


def _operation_rules(path, method, route, operation, spec, facts, structs,
                     auth_headers, spec_key=None):
    """path and method are the code's spelling, which is what a finding reports
    and what a reader will search for. spec_key is the spec's spelling of the
    same endpoint, which the two index lookups below need; the two differ
    whenever a path parameter is named differently on each side."""
    spec_key = spec_key or (path, method)
    out = []
    success_codes = spec.success_codes(spec_key)
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
            # A struct found by the spec's schema name is an assumption: that
            # the document names its schemas after the types behind them. True
            # of a generated spec, and a coincidence waiting to happen in a
            # hand-written one. Sharing not a single field name is the signature
            # of that coincidence rather than of drift - drift renames a field
            # or adds one, it does not replace every field at once - and a real
            # project was reported on exactly this: a spec schema named
            # ForwardDest against a Go config struct of the same name, where the
            # response type actually wrapped the config as one of its fields.
            if documented and not (set(documented) & set(actual)):
                actual = None
        if actual is None and facts and facts.get("response_complete") \
                and facts.get("response_fields"):
            actual = facts["response_fields"]
            struct = None

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
    documented_codes = set(spec.status_codes(spec_key))
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

    coverage = {}
    try:
        findings = audit(args.source, args.spec, args.strip_prefix, args.language,
                         args.exclude_paths, args.contract_middleware,
                         coverage=coverage)
    except (RouteExtractionError, SpecError) as exc:
        sys.exit(f"error: {exc}")

    if args.json:
        print(json.dumps({"findings": findings,
                          "meta": {"coverage": coverage}}, indent=2))
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
