"""TypeScript / JavaScript adapter — Express.

Route extraction runs through the TypeScript compiler's own parser (see
tools/tsroutes/extract.mjs) and emits the same table shape as the Go adapter, so
nothing above this layer changes.

Verification runs the real handler under `node --test` with a mock request and
response, which is the Express analogue of driving a Go handler through
`httptest`: the handler under test is the one the route registers, and nothing
about its behaviour is reimplemented.

The mock is deliberately dependency-free. Requiring supertest or a running
server would mean the gate could only verify projects that already have those,
and a gate that cannot run is a gate that lets claims through.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

NAME = "typescript"
DEFAULT_PREFIX = "/api/v1"
EXTRACTOR = pathlib.Path(__file__).resolve().parents[1] / "tools" / "tsroutes" / "extract.mjs"

VERIFICATION_SUPPORTED = True
TEST_FILENAME = "contract_verify.test.mjs"


class ExtractionError(RuntimeError):
    pass


def detect(directory):
    directory = pathlib.Path(directory)
    if (directory / "package.json").exists() or (directory.parent / "package.json").exists():
        return True
    return any(directory.rglob("*.ts")) and not (directory / "go.mod").exists()


def extract(directory, strip_prefix=DEFAULT_PREFIX):
    directory = pathlib.Path(directory).resolve()
    completed = subprocess.run(
        ["node", str(EXTRACTOR), "--dir", str(directory), "--strip-prefix", strip_prefix],
        capture_output=True, text=True, timeout=300,
    )
    if completed.returncode != 0:
        raise ExtractionError(completed.stderr.strip() or "typescript extraction failed")
    table = json.loads(completed.stdout)

    # Routes the mount graph could not place keep their own path rather than
    # being dropped. An understated surface reads as a clean bill of health,
    # which is the one wrong answer this tool must never give.
    table["unmounted"] = [r for r in table["routes"] if r["style"] == "express-unmounted"]
    return table


def test_path(api_dir, route):
    """Beside the handler module, so a relative import resolves without config."""
    return pathlib.Path(api_dir) / pathlib.Path(route["file"]).parent / TEST_FILENAME


def test_command(api_dir):
    return ["node", "--test", TEST_FILENAME]


def build_failed(output):
    return ("SyntaxError" in output or "Cannot find module" in output
            or "ERR_MODULE_NOT_FOUND" in output)


def skipped(output):
    return "# skipped 1" in output or "SKIP" in output


def handler_module(api_dir, route, table):
    """The module file that defines the handler, which is not always the file
    that registers the route."""
    facts = (table.get("handlers") or {}).get(route["handler"])
    if facts and facts.get("file"):
        return facts["file"]

    # Fall back to searching the tree for the exported name. Express projects
    # routinely register handlers imported from a sibling module.
    name = route["handler"]
    for candidate in sorted(pathlib.Path(api_dir).rglob("*.[jt]s")):
        if candidate.name.endswith((".test.js", ".test.ts", ".spec.js", ".spec.ts")):
            continue
        try:
            text = candidate.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if f"function {name}" in text or f"const {name}" in text or f"{name} =" in text:
            return str(candidate.relative_to(pathlib.Path(api_dir)))
    return None
