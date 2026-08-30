"""PHP adapter - Laravel.

Route extraction uses PHP's own tokenizer via `token_get_all` (see
tools/phproutes/extract.php), not regular expressions. A route table read
approximately is worse than none: an understated API surface reads as a clean
bill of health, which is the one wrong answer this tool must never give.

The project is never booted and `vendor/` is never required, so a Laravel app
whose dependencies are not installed can still be audited. Verification calls the
controller method directly for the same reason the other gates avoid their
frameworks: a gate that needs the target's runtime can only verify projects that
already have it.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

NAME = "php"
DEFAULT_PREFIX = "/api"
TEST_FILENAME = "contract_verify_check.php"
VERIFICATION_SUPPORTED = True
EXTRACTOR = pathlib.Path(__file__).resolve().parents[1] / "tools" / "phproutes" / "extract.php"

# The extractor now reads controller response shapes and statuses from the token
# stream, following one level of $this->helper() delegation, so the same kinds
# the other languages settle by parsing are settled here too.
DETERMINISTIC_KINDS = {
    "route_missing_from_spec", "route_missing_from_code",
    "response_field_mismatch", "response_type_mismatch",
    "status_code_mismatch", "undocumented_status",
}


class ExtractionError(RuntimeError):
    pass


def detect(directory):
    directory = pathlib.Path(directory)
    if (directory / "go.mod").exists():
        return False
    if (directory / "artisan").exists() or (directory / "composer.json").exists():
        return True
    if (directory / "routes" / "api.php").exists() or (directory / "routes" / "web.php").exists():
        return True
    from . import has_source
    return has_source(directory, ".php")


def available():
    """Whether PHP is installed. Reported rather than assumed, so a missing
    toolchain is a clear message instead of a confusing extraction failure."""
    try:
        return subprocess.run(["php", "--version"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


def extract(directory, strip_prefix=DEFAULT_PREFIX):
    if not available():
        raise ExtractionError(
            "php is not installed, and the Laravel extractor uses PHP's own "
            "tokenizer. Install PHP (brew install php) or pass --language for a "
            "different project.")
    directory = pathlib.Path(directory).resolve()
    completed = subprocess.run(
        ["php", str(EXTRACTOR), "--dir", str(directory), "--strip-prefix", strip_prefix],
        capture_output=True, text=True, timeout=300,
    )
    if completed.returncode != 0:
        raise ExtractionError(completed.stderr.strip() or "php extraction failed")
    return json.loads(completed.stdout)


def test_path(api_dir, route):
    return pathlib.Path(api_dir) / TEST_FILENAME


def test_command(api_dir):
    return ["php", TEST_FILENAME]


def build_failed(output):
    return "Parse error" in output or "Fatal error: Uncaught Error: Class" in output


def skipped(output):
    return "SKIP:" in output


def _controller_file(api_dir, route):
    """The file defining the route's controller method."""
    api_dir = pathlib.Path(api_dir)
    name = route.get("handler") or ""
    if not name:
        return None
    for candidate in sorted(api_dir.rglob("*.php")):
        if "vendor" in candidate.parts:
            continue
        try:
            text = candidate.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if f"function {name}(" in text:
            return str(candidate.relative_to(api_dir))
    return None


def handler_source(api_dir, route, table):
    """The controller method's source, with its file and line."""
    module = _controller_file(api_dir, route)
    if not module:
        return None, None, 0

    path = pathlib.Path(api_dir) / module
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return None, None, 0

    name = route["handler"]
    for index, line in enumerate(lines):
        if f"function {name}(" not in line:
            continue
        depth, started, collected = 0, False, []
        for current in lines[index:]:
            collected.append(current)
            depth += current.count("{") - current.count("}")
            if "{" in current:
                started = True
            if started and depth <= 0:
                break
            if len(collected) > 300:
                break
        return "\n".join(collected), module, index + 1

    return "\n".join(lines), module, 1


def supporting_sources(api_dir, module):
    """The whole controller, so a private helper building the response body is
    visible. Laravel controllers keep those beside the action rather than in a
    separate module, so one file is usually the whole picture."""
    path = pathlib.Path(api_dir) / module
    try:
        return [(module, path.read_text())]
    except (OSError, UnicodeDecodeError):
        return []


def controller_class(api_dir, module):
    """The fully-qualified class name for a controller file.

    PHP resolves an unqualified name against the current namespace, which for a
    generated script at the project root is the global one. Laravel controllers
    live under `App\\Http\\Controllers`, so instantiating the bare name fails with
    "Class not found" - an environment error that would otherwise be reported as
    a failed verification.
    """
    import re

    path = pathlib.Path(api_dir) / module
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return pathlib.Path(module).stem

    namespace = ""
    match = re.search(r"^\s*namespace\s+([^;]+);", text, re.M)
    if match:
        namespace = match.group(1).strip()

    match = re.search(r"^\s*(?:final\s+|abstract\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.M)
    name = match.group(1) if match else pathlib.Path(module).stem
    return f"\\{namespace}\\{name}" if namespace else name
