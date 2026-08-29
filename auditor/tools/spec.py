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
        global_security = self._security_names(self.document.get("security", []))

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


def load(path):
    path = pathlib.Path(path)
    if not path.exists():
        raise SpecError(f"spec not found: {path}")
    try:
        with open(path) as f:
            document = json.load(f)
    except json.JSONDecodeError as exc:
        raise SpecError(f"{path} is not valid JSON: {exc}") from exc
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
