#!/usr/bin/env python3
"""Route table extraction - component 1 of the auditor.

Thin Python wrapper over the `goroutes` Go program, which does the actual AST
walk. The split is deliberate: parsing Go is a job for the Go standard library,
and the rest of the auditor is Python.

    from auditor.tools.routes import extract
    table = extract("eval/fixture", strip_prefix="/v1")

Or standalone:

    python3 auditor/tools/routes.py eval/fixture --strip-prefix /v1
    python3 auditor/tools/routes.py eval/fixture --strip-prefix /v1 --json
"""

import argparse
import json
import pathlib
import subprocess
import sys

TOOL_DIR = pathlib.Path(__file__).resolve().parent / "goroutes"
BINARY = TOOL_DIR / "goroutes"


class RouteExtractionError(RuntimeError):
    """Raised when the source could not be parsed. Never returns a partial table:
    a silently truncated route table would understate the API surface, which is
    the exact error this component exists to prevent."""


def ensure_built(force=False):
    """Build the extractor if the binary is missing or older than its source."""
    sources = sorted(TOOL_DIR.glob("*.go"))
    if not sources:
        raise RouteExtractionError(f"no Go source found in {TOOL_DIR}")

    if not force and BINARY.exists():
        newest = max(s.stat().st_mtime for s in sources)
        if BINARY.stat().st_mtime >= newest:
            return BINARY

    result = subprocess.run(
        ["go", "build", "-o", str(BINARY), "."],
        cwd=TOOL_DIR, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RouteExtractionError(f"go build failed:\n{result.stderr.strip()}")
    return BINARY


def extract(directory, strip_prefix=""):
    """Return the route table for a directory of Go source.

    The result is a dict with `routes`, `annotations_unrouted`, `route_count`
    and `routes_without_annotation`. Each route carries its file and line, so
    every downstream finding can cite a location rather than assert one.
    """
    directory = pathlib.Path(directory).resolve()
    if not directory.is_dir():
        raise RouteExtractionError(f"not a directory: {directory}")

    binary = ensure_built()
    cmd = [str(binary), "-dir", str(directory)]
    if strip_prefix:
        cmd += ["-strip-prefix", strip_prefix]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RouteExtractionError(result.stderr.strip() or "extraction failed")

    return json.loads(result.stdout)


def index_by_operation(table):
    """Key the table by (path, lowercase method) - the shape the spec index and
    the differ both consume."""
    return {(r["path"], r["method"].lower()): r for r in table["routes"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory", help="directory of Go source to scan")
    parser.add_argument("--strip-prefix", default="", help="path prefix to strip, e.g. /v1")
    parser.add_argument("--json", action="store_true", help="emit the raw table")
    parser.add_argument("--rebuild", action="store_true", help="force rebuild of the extractor")
    args = parser.parse_args()

    if args.rebuild:
        ensure_built(force=True)

    try:
        table = extract(args.directory, args.strip_prefix)
    except RouteExtractionError as exc:
        sys.exit(f"error: {exc}")

    if args.json:
        print(json.dumps(table, indent=2))
        return

    print(f"{table['route_count']} routes in {args.directory}"
          f"  ({table['routes_without_annotation']} without an annotation,"
          f" {len(table['annotations_unrouted'])} annotation(s) with no route)\n")
    print(f"{'METHOD':<7} {'PATH':<26} {'HANDLER':<18} {'ANNOTATION':<22} AUTH")
    for route in table["routes"]:
        ann = route["annotation"]
        claim = f"{ann['method']} {ann['path']}" if ann else "- none -"
        auth = ",".join(ann["security"]) if ann and ann["security"] else "public"
        print(f"{route['method']:<7} {route['path']:<26} {route['handler']:<18} {claim:<22} {auth}")

    for orphan in table["annotations_unrouted"]:
        print(f"\n  annotation with no route: {orphan['method'].upper()} {orphan['path']} "
              f"({orphan['file']}:{orphan['line']})")


if __name__ == "__main__":
    main()
