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
        routes, mounts, imports = [], [], {}

        for node in ast.walk(tree):
            # `from .customers import router as customers_router`
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, "module", None) or ""
                for alias in node.names:
                    imports[alias.asname or alias.name] = module

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

        per_file[rel] = {"routes": routes, "mounts": mounts, "imports": imports}
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
            full = normalise(prefix + route["path"], args.strip_prefix)
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
        "routes": routes, "annotations_unrouted": [], "structs": {}, "handlers": {},
        "route_count": len(routes), "routes_without_annotation": len(routes),
    }, indent=2))


if __name__ == "__main__":
    main()
