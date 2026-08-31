#!/usr/bin/env python3
"""OpenAPI index: component 2 of the auditor.

Loads a spec into the same `(path, method)` shape the route table uses, with
`$ref` resolved, so the two can be compared directly. Deterministic; no model.

    from auditor.tools.spec import load
    index = load("eval/fixture/spec/openapi.json")
    op = index[("/payouts", "post")]

Standalone:

    python3 auditor/tools/spec.py eval/fixture/spec/openapi.json
"""

import argparse
import json
import pathlib
import sys

METHODS = ("get", "post", "put", "patch", "delete", "head", "options")

# Validation keywords carried through the index verbatim.
#
# These were dropped in the first version, and it cost precision immediately: the
# auditor showed a reviewer `bvn: {"type": "string"}` for a field the spec
# constrains to exactly 11 characters, and a correct reading of that incomplete
# picture produced a finding that was not true. Starving a reader of context does
# not make it cautious - it makes it confidently wrong.
CONSTRAINT_KEYS = (
    "minLength", "maxLength", "pattern", "enum",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "multipleOf", "minItems", "maxItems", "uniqueItems", "default", "nullable",
)


def constraints(schema):
    """Every validation keyword the schema declares, omitting the ones it does
    not - so an absent constraint is visibly absent rather than implied."""
    if not isinstance(schema, dict):
        return {}
    return {k: schema[k] for k in CONSTRAINT_KEYS if k in schema}


class SpecError(RuntimeError):
    """Raised when the spec cannot be read. As with route extraction, a partial
    index is worse than none: it would silently understate what the spec
    promises and turn missing documentation into a clean bill of health."""


class Spec:
    """An indexed OpenAPI document."""

    def __init__(self, document, source=""):
        self.document = document
        self.source = source
        self.components = document.get("components", {}).get("schemas", {})
        self.operations = {}
        # Computed once. The status rules ask for it per operation, and the
        # answer is a property of the whole document.
        self._generator = None
        self._index()

    # -- $ref resolution -------------------------------------------------

    def resolve(self, schema, depth=0):
        """Follow local $refs. Depth-limited so a recursive schema terminates
        instead of hanging the audit."""
        if not isinstance(schema, dict) or depth > 20:
            return schema if isinstance(schema, dict) else {}
        ref = schema.get("$ref")
        if not ref:
            return schema
        if not ref.startswith("#/components/schemas/"):
            return {}
        name = ref.rsplit("/", 1)[-1]
        return self.resolve(self.components.get(name, {}), depth + 1)

    def schema_name(self, schema):
        """The referenced component name, when the schema is a plain $ref."""
        if isinstance(schema, dict):
            ref = schema.get("$ref")
            if ref and ref.startswith("#/components/schemas/"):
                return ref.rsplit("/", 1)[-1]
        return None

    def properties(self, schema):
        """Flatten a schema to `{json name: {type, format, required}}`.

        Arrays are described by their item schema, since the wire contract for a
        list is the shape of its elements. Composition keywords are merged
        shallowly; a property defined in more than one branch keeps the first
        definition, and callers that need certainty should check `composed`.
        """
        resolved = self.resolve(schema)
        if not resolved:
            return {}, False

        if resolved.get("type") == "array" or "items" in resolved:
            return self.properties(resolved.get("items", {}))

        composed = False
        merged = dict(resolved.get("properties", {}))
        required = set(resolved.get("required", []))
        for keyword in ("allOf", "oneOf", "anyOf"):
            for branch in resolved.get(keyword, []):
                composed = True
                sub = self.resolve(branch)
                for key, value in sub.get("properties", {}).items():
                    merged.setdefault(key, value)
                required |= set(sub.get("required", []))

        out = {}
        for name, definition in merged.items():
            definition = self.resolve(definition)
            entry = {
                "type": definition.get("type", ""),
                "format": definition.get("format", ""),
                "required": name in required,
            }
            entry.update(constraints(definition))
            out[name] = entry
        return out, composed

    # -- indexing --------------------------------------------------------

    def _index(self):
        # No default. A document with no `security` key has not declared its
        # operations public - it has said nothing about authentication at all,
        # and the two are opposite claims. Defaulting to [] read every such
        # document as "everything here is public", so every guarded route in it
        # became an endpoint the spec supposedly promised needed no credential.
        # Specs that declare securitySchemes and then never reference them are
        # common, which is exactly where this fired.
        global_security = self._security_names(self.document.get("security"))

        for path, item in self.document.get("paths", {}).items():
            if not isinstance(item, dict):
                continue
            shared = item.get("parameters", [])
            for method in METHODS:
                operation = item.get(method)
                if not isinstance(operation, dict):
                    continue
                self.operations[(path, method)] = self._operation(
                    path, method, operation, shared, global_security
                )

    def _operation(self, path, method, operation, shared, global_security):
        params = []
        for raw in list(shared) + list(operation.get("parameters", [])):
            raw = self.resolve(raw) if "$ref" in raw else raw
            schema = raw.get("schema", {})
            param = {
                "name": raw.get("name", ""),
                "in": raw.get("in", ""),
                "required": bool(raw.get("required", False)),
                "type": schema.get("type", ""),
                "default": schema.get("default", None),
            }
            param.update(constraints(schema))
            params.append(param)

        request = operation.get("requestBody", {})
        request_schema = (request.get("content", {})
                                 .get("application/json", {})
                                 .get("schema", {}))
        request_props, request_composed = self.properties(request_schema)

        responses = {}
        for code, response in operation.get("responses", {}).items():
            schema = (response.get("content", {})
                              .get("application/json", {})
                              .get("schema", {}))
            props, composed = self.properties(schema)
            responses[str(code)] = {
                "description": response.get("description", ""),
                "schema_name": self.schema_name(schema)
                               or self.schema_name(self.resolve(schema).get("items", {})),
                "is_array": self.resolve(schema).get("type") == "array",
                "properties": props,
                "composed": composed,
                "headers": sorted(response.get("headers", {}).keys()),
            }

        security = self._security_names(operation.get("security", None))
        if security is None:
            security = global_security

        return {
            "path": path,
            "method": method,
            "operation_id": operation.get("operationId", ""),
            "summary": operation.get("summary", ""),
            "description": operation.get("description", ""),
            "params": params,
            "request_schema_name": self.schema_name(request_schema),
            "request_properties": request_props,
            "request_composed": request_composed,
            "request_required": bool(request.get("required", False)),
            "responses": responses,
            "security": security,
        }

    @staticmethod
    def _security_names(security):
        """`security: []` on an operation means explicitly public, which is not
        the same as absent. Returning None for absent keeps that distinction, so
        a caller can fall back to the document-level default correctly."""
        if security is None:
            return None
        names = []
        for requirement in security:
            names.extend(requirement.keys())
        return sorted(set(names))

    # -- accessors -------------------------------------------------------

    def __getitem__(self, key):
        return self.operations[key]

    def __contains__(self, key):
        return key in self.operations

    def __len__(self):
        return len(self.operations)

    def keys(self):
        return self.operations.keys()

    def items(self):
        return self.operations.items()

    def status_codes(self, key):
        return sorted(c for c in self.operations[key]["responses"] if c.isdigit())

    def success_codes(self, key):
        return [c for c in self.status_codes(key) if c.startswith("2")]

    def info(self):
        """title, version, description and server urls, as strings.

        What a framework's schema generator copies out of the application object
        it was pointed at, and therefore what identifies which application a
        generated document describes.
        """
        info = self.document.get("info") or {}
        servers = [str(s.get("url", "")) for s in (self.document.get("servers") or [])
                   if isinstance(s, dict)]
        return {"title": str(info.get("title", "")),
                "version": str(info.get("version", "")),
                "description": str(info.get("description", "")),
                "servers": [u for u in servers if u]}

    def generated_by(self):
        """The framework whose schema generator wrote this document, if one did.

        This changes what an absent response means. A hand-written document
        omits a status because nobody wrote it down, and that is drift worth
        reporting. A generated one omits every status the framework was not
        explicitly told to declare - FastAPI never emits a 5xx unless the route
        declares `responses={500: ...}` - so the two sides never agreed and
        never could. Reporting that as drift is a false positive on every run
        of every project of that shape, which teaches a reader to skim past the
        category rather than read it.

        FastAPI's operationId is `{function}_{path}_{method}`, which is why the
        method suffix is the fingerprint: no hand-written document names its
        operations that way twice by accident.
        """
        if self._generator is not None:
            return self._generator
        self._generator = self._detect_generator()
        return self._generator

    def _detect_generator(self):
        generator = str((self.document.get("info") or {}).get("x-generator", ""))
        if "fastapi" in generator.lower():
            return "fastapi"
        ids = [op["operation_id"] for op in self.operations.values() if op["operation_id"]]
        if len(ids) < 1 or len(ids) < len(self.operations):
            return ""
        suffixes = tuple(f"_{m}" for m in
                         ("get", "post", "put", "patch", "delete", "head", "options"))
        if all(i.endswith(suffixes) for i in ids):
            return "fastapi"
        return ""


def _load_json(text, path):
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpecError(f"{path} is not valid JSON: {exc}") from exc


def _load_yaml(text, path):
    try:
        import yaml
    except ImportError as exc:                              # pragma: no cover
        raise SpecError(
            f"{path} is YAML and no YAML parser is installed. Install one "
            f"(apt install python3-yaml, or pip install pyyaml) or point the "
            f"spec input at a JSON document.") from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpecError(f"{path} is not valid YAML: {exc}") from exc


def load(path):
    """Read an OpenAPI document, in either of the two formats it is written in.

    YAML is not a nicety here. The loader was JSON-only while the file names it
    is asked for have always included openapi.yaml, so a repository whose spec
    was YAML - four of the first twelve real ones this was pointed at - failed
    with "no OpenAPI document found", which is both wrong and unactionable.

    The suffix chooses which parser to try first, not which one is right. A
    project that generates openapi.json from a YAML source, or serves a
    .yaml written as JSON, is ordinary; whichever parser succeeds wins, and the
    error kept is the one for the format the name promised.
    """
    path = pathlib.Path(path)
    if not path.exists():
        raise SpecError(f"spec not found: {path}")
    text = path.read_text(encoding="utf-8", errors="replace")

    order = ((_load_yaml, _load_json) if path.suffix.lower() in (".yaml", ".yml")
             else (_load_json, _load_yaml))

    document, first_error = None, None
    for parse in order:
        try:
            document = parse(text, path)
            break
        except SpecError as exc:
            first_error = first_error or exc
    if document is None:
        raise first_error

    if not isinstance(document, dict):
        raise SpecError(f"{path} parsed but is not an object; not an OpenAPI "
                        f"document?")
    if "paths" not in document:
        raise SpecError(f"{path} has no `paths`; not an OpenAPI document?")
    return Spec(document, source=str(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("spec", help="path to openapi.json")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        spec = load(args.spec)
    except SpecError as exc:
        sys.exit(f"error: {exc}")

    if args.json:
        print(json.dumps(spec.operations, indent=2, default=str))
        return

    print(f"{len(spec)} operations in {args.spec}\n")
    print(f"{'METHOD':<7} {'PATH':<24} {'2XX':<6} {'AUTH':<12} {'PARAMS':<7} RESPONSE")
    for (path, method), op in sorted(spec.items()):
        success = ",".join(spec.success_codes((path, method))) or "none"
        auth = ",".join(op["security"]) if op["security"] else "public"
        body = op["responses"].get(success.split(",")[0], {})
        shape = body.get("schema_name") or "none"
        if body.get("is_array"):
            shape += "[]"
        print(f"{method.upper():<7} {path:<24} {success:<6} {auth:<12} "
              f"{len(op['params']):<7} {shape}")


if __name__ == "__main__":
    main()
