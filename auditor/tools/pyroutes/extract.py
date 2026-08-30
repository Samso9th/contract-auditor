#!/usr/bin/env python3
"""Extract an HTTP route table from a Python project using the `ast` module.

Emits the same JSON shape as the Go and TypeScript extractors, so every layer
above it is unchanged.

Unlike the TypeScript extractor this needs nothing installed: Python parses
Python. The target project's own dependencies are never imported, so a project
whose requirements are not installed can still be audited.

Recognised:

    FastAPI   @app.get("/x")            @router.post("/x", status_code=201)
              app.include_router(router, prefix="/v1")
    Flask     @app.route("/x", methods=["GET", "POST"])
              app.register_blueprint(bp, url_prefix="/v1")

Usage: python3 extract.py --dir <src> [--strip-prefix /api/v1]
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import sys

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
SKIP_DIRS = {"venv", ".venv", "env", "node_modules", "__pycache__", ".git",
             "site-packages", "migrations", "build", "dist", ".tox"}

# FastAPI and Flask both spell path parameters "{name}", which is already the
# OpenAPI form. Flask's converters ("<int:id>") are not.
FLASK_PARAM = re.compile(r"<(?:[^:>]+:)?([^>]+)>")


def source_files(root):
    out = []
    for path in sorted(pathlib.Path(root).rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name.startswith("test_") or path.name.endswith("_test.py"):
            continue
        out.append(path)
    return out


def literal(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def keyword_value(call, name):
    for kw in call.keywords:
        if kw.arg == name:
            if isinstance(kw.value, ast.Constant):
                return kw.value.value
            if isinstance(kw.value, (ast.List, ast.Tuple)):
                return [literal(e) for e in kw.value.elts if literal(e)]
    return None


def normalise(path, strip_prefix):
    path = FLASK_PARAM.sub(r"{\1}", path)
    if strip_prefix and path.startswith(strip_prefix):
        trimmed = path[len(strip_prefix):]
        if trimmed.startswith("/"):
            path = trimmed
    path = re.sub(r"/{2,}", "/", path)
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


def receiver(node):
    """The object a decorator or call hangs off: `router` in `router.get(...)`."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


# Handler-body facts, the Python counterpart of facts.go and the TypeScript
# extractor. Without them the rules settle only route existence and status codes,
# and every response shape falls to a model reading source approximately.

JSON_TYPE_BY_NODE = {
    ast.Str: "string", ast.Num: "number",
}


def json_type_of(node):
    """The JSON type of an expression where the AST settles it."""
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, str):
            return "string"
        if isinstance(value, (int, float)):
            return "number"
        if value is None:
            return "null"
    if isinstance(node, (ast.List, ast.Tuple, ast.ListComp)):
        return "array"
    if isinstance(node, (ast.Dict, ast.DictComp)):
        return "object"
    if isinstance(node, ast.JoinedStr):
        return "string"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return {"str": "string", "int": "number", "float": "number",
                "bool": "boolean", "list": "array", "dict": "object"}.get(node.func.id, "")
    return ""


def fields_of_dict(node):
    """Keys of a dict literal with the JSON type of each value.

    A `**spread` or a computed key makes the shape incomplete, and an incomplete
    shape must not be compared: a field the extractor could not see would read as
    a field the handler failed to return.
    """
    fields, complete = {}, True
    for key, value in zip(node.keys, node.values):
        if key is None:
            complete = False
            continue
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            complete = False
            continue
        fields[key.value] = {
            "json_name": key.value,
            "json_type": json_type_of(value),
            "line": getattr(key, "lineno", 0),
        }
    return fields, complete


def index_functions(tree):
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, node)
    return out


def returned_shape(fn, functions, depth=0):
    """The dict a function returns, following one level of local helper."""
    if fn is None or depth > 2:
        return None
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        value = node.value
        # `return 201, {...}` - the explicit status form.
        if isinstance(value, ast.Tuple) and len(value.elts) == 2:
            value = value.elts[1]
        if isinstance(value, ast.Dict):
            return fields_of_dict(value)
        if isinstance(value, (ast.List, ast.Tuple)) and value.elts and isinstance(value.elts[0], ast.Dict):
            return fields_of_dict(value.elts[0])
        # `return helper(...)` and `return handlers.helper(...)`. The second is
        # the common FastAPI shape - a thin route function delegating to a
        # service - and not following it leaves the response shape invisible.
        if isinstance(value, ast.Call):
            target = None
            if isinstance(value.func, ast.Name):
                target = value.func.id
            elif isinstance(value.func, ast.Attribute):
                target = value.func.attr
            if target:
                helper = functions.get(target)
                if helper is not None and helper is not fn:
                    return returned_shape(helper, functions, depth + 1)
    return None


def collect(root):
    """Per file: routes declared, and routers mounted under a prefix."""
    files = source_files(root)
    per_file = {}

    for path in files:
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        rel = str(path.relative_to(root))
        routes, mounts, imports, own_prefixes = [], [], {}, {}

        for node in ast.walk(tree):
            # `from .customers import router as customers_router`
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, "module", None) or ""
                for alias in node.names:
                    imports[alias.asname or alias.name] = module

            # `router = APIRouter(prefix="/workspaces")`, and the Flask
            # Blueprint equivalent. A router carries its own prefix as well as
            # the one it is mounted under, and both frameworks concatenate the
            # two. Reading only the mount lost the inner half of every path in
            # a project that declares it this way - 52 of one repository's 53
            # endpoints - and that is the documented FastAPI idiom for a
            # versioned API, not an unusual style.
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                called = node.value.func
                constructor = (called.attr if isinstance(called, ast.Attribute)
                               else getattr(called, "id", ""))
                if constructor in ("APIRouter", "Blueprint"):
                    own = (keyword_value(node.value, "prefix")
                           or keyword_value(node.value, "url_prefix") or "")
                    if isinstance(own, str) and own:
                        for target in node.targets:
                            if isinstance(target, ast.Name):
                                own_prefixes[target.id] = own

            # include_router / register_blueprint carry the prefix.
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                name = node.func.attr
                if name in ("include_router", "register_blueprint") and node.args:
                    prefix = keyword_value(node, "prefix") or keyword_value(node, "url_prefix") or ""
                    mounts.append({"prefix": prefix or "",
                                   "target": receiver(node.args[0])})

            # Decorated handlers.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Call):
                        continue
                    func = decorator.func
                    if not isinstance(func, ast.Attribute) or not decorator.args:
                        continue
                    route_path = literal(decorator.args[0])
                    if route_path is None:
                        continue

                    verb = func.attr.lower()
                    owner = receiver(func.value)

                    if verb in HTTP_METHODS:
                        methods = [verb.upper()]
                    elif verb in ("route", "add_route"):
                        declared = keyword_value(decorator, "methods")
                        methods = [m.upper() for m in (declared or ["GET"]) if m]
                    else:
                        continue

                    status = keyword_value(decorator, "status_code")
                    # The generated test calls the handler directly, so it needs
                    # the real parameter names; guessing them produces a
                    # TypeError that looks like a failed verification.
                    arg_names = [a.arg for a in node.args.args
                                 if a.arg not in ("self", "cls")]
                    for method in methods:
                        routes.append({
                            "method": method, "path": route_path, "owner": owner,
                            "handler": node.name, "line": node.lineno,
                            "status_code": status,
                            "is_async": isinstance(node, ast.AsyncFunctionDef),
                            "args": arg_names,
                        })

        per_file[rel] = {"routes": routes, "mounts": mounts, "imports": imports,
                         "own_prefixes": own_prefixes, "tree": tree, "path": path}
    return per_file


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default=".")
    parser.add_argument("--strip-prefix", default="")
    args = parser.parse_args()

    root = pathlib.Path(args.dir).resolve()
    if not root.is_dir():
        print(json.dumps({"error": f"not a directory: {root}"}))
        sys.exit(1)

    per_file = collect(root)

    # Every function in the project, so a handler that builds its response in an
    # imported module can be followed. First declaration in sorted file order
    # wins, which keeps the result stable across runs.
    global_functions, defined_in = {}, {}
    for rel in sorted(per_file):
        for name, node in index_functions(per_file[rel]["tree"]).items():
            if name not in global_functions:
                global_functions[name] = node
                defined_in[name] = rel

    # Keyed by name for compatibility, and by "file::name" as well. A route
    # function and the service it delegates to routinely share a name in
    # different modules; keyed only by name they collide, and the ambiguity guard
    # then declines both - safe, but it costs every response-shape rule.
    handlers, by_location = {}, {}
    for rel in sorted(per_file):
        for name, node in index_functions(per_file[rel]["tree"]).items():
            duplicate = name in handlers and handlers[name]["file"] != rel

            statuses, queries, headers_set = set(), set(), set()
            for inner in ast.walk(node):
                # Only where an integer is unambiguously a status: raised as the
                # first argument of an exception, returned as the first element
                # of a (status, body) tuple, or named status_code. Treating any
                # integer first-argument as a status would turn `range(200)` or
                # a page size into a documented response.
                if isinstance(inner, ast.Raise) and isinstance(inner.exc, ast.Call):
                    for arg in inner.exc.args[:1]:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, int) \
                                and 100 <= arg.value < 600:
                            statuses.add(arg.value)
                    for kw in inner.exc.keywords:
                        if kw.arg in ("status", "status_code") \
                                and isinstance(kw.value, ast.Constant) \
                                and isinstance(kw.value.value, int):
                            statuses.add(kw.value.value)
                if isinstance(inner, ast.Return) and isinstance(inner.value, ast.Tuple) \
                        and inner.value.elts and isinstance(inner.value.elts[0], ast.Constant) \
                        and isinstance(inner.value.elts[0].value, int) \
                        and 100 <= inner.value.elts[0].value < 600:
                    statuses.add(inner.value.elts[0].value)
                # `request.args.get("page")` (Flask) and `request.query_params.get(...)`
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) \
                        and inner.func.attr == "get" and inner.args \
                        and isinstance(inner.args[0], ast.Constant) \
                        and isinstance(inner.func.value, ast.Attribute) \
                        and inner.func.value.attr in ("args", "query_params"):
                    queries.add(inner.args[0].value)
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) \
                        and inner.func.attr in ("set", "add") and len(inner.args) >= 1 \
                        and isinstance(inner.args[0], ast.Constant) \
                        and isinstance(inner.args[0].value, str) \
                        and isinstance(inner.func.value, ast.Attribute) \
                        and inner.func.value.attr == "headers":
                    headers_set.add(inner.args[0].value)

            shape = returned_shape(node, global_functions)
            ordered = sorted(statuses)
            fact = {
                "name": name,
                "statuses": ordered,
                "success_code": next((s for s in ordered if 200 <= s < 300), 0),
                "query_params": sorted(queries),
                "headers_read": [],
                "headers_set": sorted(headers_set),
                "response_fields": shape[0] if shape else {},
                "response_complete": bool(shape and shape[1]),
                "file": rel,
                "line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "ambiguous": False,
            }
            by_location[f"{rel}::{name}"] = fact
            if name in handlers:
                if duplicate:
                    handlers[name]["ambiguous"] = True
                continue
            handlers[name] = fact

    # A router's prefix is declared where it is mounted, not where its routes
    # are. Prefixes are collected by the variable the routes hang off, which
    # covers the common single-mount case without pretending to resolve
    # cross-module aliasing the AST cannot settle.
    prefixes = {}
    for data in per_file.values():
        for mount in data["mounts"]:
            if mount["target"]:
                prefixes.setdefault(mount["target"], mount["prefix"])

    routes, seen = [], set()
    for rel, data in sorted(per_file.items()):
        for route in data["routes"]:
            prefix = prefixes.get(route["owner"], "")
            own = data["own_prefixes"].get(route["owner"], "")
            full = normalise(prefix + own + route["path"], args.strip_prefix)
            key = (route["method"], full)
            if key in seen:
                continue
            seen.add(key)
            routes.append({
                "method": route["method"], "path": full,
                "handler": route["handler"], "file": rel, "line": route["line"],
                "style": "python", "annotation": None,
                "status_code": route["status_code"], "is_async": route["is_async"],
                "args": route.get("args", []),
            })

    routes.sort(key=lambda r: (r["path"], r["method"]))
    print(json.dumps({
        "dir": str(root), "strip_prefix": args.strip_prefix, "language": "python",
        "routes": routes, "annotations_unrouted": [], "structs": {},
        "handlers": handlers, "handlers_by_location": by_location,
        "route_count": len(routes), "routes_without_annotation": len(routes),
    }, indent=2))


if __name__ == "__main__":
    main()
