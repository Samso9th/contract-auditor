"""Python adapter - FastAPI, Flask, and anything using the same decorator idiom.

Route extraction uses the `ast` module, so nothing needs installing and the
target project's own dependencies are never imported. A project whose
requirements are missing can still be audited, which is not true of any approach
that imports the app to inspect it.

Verification calls the handler directly rather than through a test client, for
the same reason the TypeScript gate uses a mock request: a gate that needs
FastAPI's TestClient installed can only verify projects that already have it, and
a gate that cannot run is a gate that lets claims through.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

NAME = "python"
DEFAULT_PREFIX = "/api/v1"
TEST_FILENAME = "contract_verify_check.py"
VERIFICATION_SUPPORTED = True
EXTRACTOR = pathlib.Path(__file__).resolve().parents[1] / "tools" / "pyroutes" / "extract.py"

# The extractor emits routes and the decorator's status_code, but no response
# shapes, so the rules can settle route existence and status codes. Everything
# else falls to the agent, and the gate verifies it either way.
DETERMINISTIC_KINDS = {"route_missing_from_spec", "route_missing_from_code",
                       "status_code_mismatch"}


class ExtractionError(RuntimeError):
    pass


def detect(directory):
    directory = pathlib.Path(directory)
    if (directory / "go.mod").exists() or (directory / "package.json").exists():
        return False
    markers = ("requirements.txt", "pyproject.toml", "setup.py", "Pipfile")
    if any((directory / m).exists() or (directory.parent / m).exists() for m in markers):
        return True
    return any(directory.rglob("*.py"))


def extract(directory, strip_prefix=DEFAULT_PREFIX):
    directory = pathlib.Path(directory).resolve()
    completed = subprocess.run(
        ["python3", str(EXTRACTOR), "--dir", str(directory), "--strip-prefix", strip_prefix],
        capture_output=True, text=True, timeout=300,
    )
    if completed.returncode != 0:
        raise ExtractionError(completed.stderr.strip() or "python extraction failed")
    return json.loads(completed.stdout)


def test_path(api_dir, route):
    """At the project root, so a package-relative import of the handler resolves
    the same way the application's own entry point resolves it."""
    return pathlib.Path(api_dir) / TEST_FILENAME


def test_command(api_dir):
    return ["python3", TEST_FILENAME]


def build_failed(output):
    return ("SyntaxError" in output or "ModuleNotFoundError" in output
            or "ImportError" in output)


def skipped(output):
    return "SKIP:" in output


def handler_source(api_dir, route, table):
    """Source of one handler, plus the module it delegates to.

    FastAPI route functions are usually thin wrappers over a service function,
    so the decorated body alone rarely shows the response shape.
    """
    api_dir = pathlib.Path(api_dir)
    path = api_dir / route["file"]
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return None, None, 0

    start = max(route["line"] - 1, 0)
    # Walk back over the decorators so the status_code and path are visible.
    while start > 0 and lines[start - 1].lstrip().startswith("@"):
        start -= 1

    body, indent = [], None
    for line in lines[start:]:
        if line.strip() and indent is not None:
            current = len(line) - len(line.lstrip())
            if current <= indent and not line.lstrip().startswith(("@", ")", "]", "}")):
                break
        if line.lstrip().startswith("def ") or line.lstrip().startswith("async def "):
            indent = len(line) - len(line.lstrip())
        body.append(line)
        if len(body) > 200:
            break

    return "\n".join(body), route["file"], route["line"]


def supporting_sources(api_dir, module):
    """Modules the handler's own module imports from the same package."""
    api_dir = pathlib.Path(api_dir)
    path = api_dir / module
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return []

    import ast as _ast
    try:
        tree = _ast.parse(text)
    except SyntaxError:
        return []

    out, budget = [], 20_000
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.ImportFrom) or node.level == 0:
            continue
        for alias in node.names:
            for candidate in ((path.parent / f"{node.module or alias.name}.py"),
                              (path.parent / f"{alias.name}.py")):
                if not candidate.exists():
                    continue
                try:
                    source = candidate.read_text()
                except (OSError, UnicodeDecodeError):
                    continue
                if len(source) > budget:
                    continue
                budget -= len(source)
                out.append((str(candidate.relative_to(api_dir)), source))
                break
    return out
