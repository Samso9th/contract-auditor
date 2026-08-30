"""TypeScript / JavaScript adapter: Express.

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
import re
import subprocess

NAME = "typescript"
DEFAULT_PREFIX = "/api/v1"

# The extractor now emits handler-body facts - response shapes resolved across
# files, statuses read from the response call's own receiver, headers set, query
# parameters read - so the same kinds Go settles by parsing are settled here too.
#
# This list is the handover point: a kind here is the rules' job and the agent is
# not asked about it. Moving a kind onto this list is how a language gets more
# reliable, because a parser reads the source exactly where a model reads it
# approximately.
DETERMINISTIC_KINDS = {
    "route_missing_from_spec", "route_missing_from_code",
    "response_field_mismatch", "response_type_mismatch", "response_header_mismatch",
    "request_param_mismatch", "status_code_mismatch", "undocumented_status",
}
EXTRACTOR = pathlib.Path(__file__).resolve().parents[1] / "tools" / "tsroutes" / "extract.mjs"

VERIFICATION_SUPPORTED = True
TEST_FILENAME = "contract_verify.test.mjs"


class ExtractionError(RuntimeError):
    pass


def detect(directory):
    directory = pathlib.Path(directory)
    if (directory / "package.json").exists() or (directory.parent / "package.json").exists():
        return True
    from . import has_source
    return has_source(directory, ".ts") and not (directory / "go.mod").exists()


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


def handler_source(api_dir, route, table):
    """The source of one handler, with its file and line.

    The Go adapter gets this from AST facts. Here the route only records where
    the handler was *registered*, so the defining module is located first and the
    function sliced out of it by a brace scan. Slicing rather than sending the
    whole module keeps the agent answering about the function it was asked about
    instead of a neighbour.

    Returns (source, file, line) or (None, None, 0).
    """
    module = handler_module(api_dir, route, table)
    if not module:
        return None, None, 0

    path = pathlib.Path(api_dir) / module
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return None, None, 0

    name = route["handler"]
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not any(marker in line for marker in
                   (f"function {name}", f"const {name}", f"let {name}", f"{name}:")):
            continue

        depth, started, collected = 0, False, []
        for current in lines[index:]:
            collected.append(current)
            depth += current.count("{") - current.count("}")
            if "{" in current:
                started = True
            if started and depth <= 0:
                break
            if len(collected) > 400:      # runaway guard
                break
        return "\n".join(collected), module, index + 1

    # The name resolves to the module but not to a slice - send the module and
    # say so, rather than sending nothing and reporting the endpoint as clean.
    return text, module, 1


# Express handlers routinely delegate the response shape to a helper in another
# module (`res.json(balanceResponse())`). Sending only the handler slice means
# the agent never sees the shape it is being asked about, and reports a clean
# endpoint because it genuinely saw nothing wrong. One level of local imports is
# enough for the common case without flooding the context.
IMPORT_PATTERN = re.compile(r"""^\s*import\s+.*?from\s+['"](\.[^'"]+)['"]""", re.M)
MAX_SUPPORTING_CHARS = 20_000


def supporting_sources(api_dir, module):
    """Source of the modules the handler's own module imports locally."""
    api_dir = pathlib.Path(api_dir)
    path = api_dir / module
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return []

    out, budget = [], MAX_SUPPORTING_CHARS
    for spec in dict.fromkeys(IMPORT_PATTERN.findall(text)):
        base = (path.parent / spec.replace(".js", "")).resolve()
        for candidate in (f"{base}.ts", f"{base}.js", f"{base}.mjs",
                          str(base / "index.ts"), str(base / "index.js")):
            target = pathlib.Path(candidate)
            if not target.exists():
                continue
            try:
                body = target.read_text()
            except (OSError, UnicodeDecodeError):
                break
            if len(body) > budget:
                break
            budget -= len(body)
            try:
                label = str(target.relative_to(api_dir))
            except ValueError:
                label = target.name
            out.append((label, body))
            break
    return out
