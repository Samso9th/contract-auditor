"""Go adapter: net/http ServeMux, gin, echo, chi.

Wraps the `go/ast` extractor in tools/goroutes and generates tests that drive a
handler through `net/http/httptest`, so the probe exercises the real handler
rather than a reimplementation of it.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

NAME = "go"
DEFAULT_PREFIX = "/v1"

# What the deterministic rules settle for this language, and therefore what the
# agent must not restate. The Go extractor emits struct shapes and handler-body
# facts, so the rules cover response shapes, status codes, query parameters,
# auth and headers without a model.
DETERMINISTIC_KINDS = {
    "route_missing_from_spec", "route_missing_from_code",
    "response_field_mismatch", "response_type_mismatch", "response_header_mismatch",
    "request_param_mismatch", "status_code_mismatch", "undocumented_status",
    "auth_mismatch",
}
TEST_FILENAME = "contract_verify_test.go"


def detect(directory):
    directory = pathlib.Path(directory)
    return (directory / "go.mod").exists() or any(directory.rglob("*.go"))


def extract(directory, strip_prefix=DEFAULT_PREFIX):
    from routes import extract as go_extract
    return go_extract(directory, strip_prefix=strip_prefix)


def test_path(api_dir, route):
    """Beside the handler, so the generated test is in the same package and can
    call an unexported handler directly."""
    return pathlib.Path(api_dir) / pathlib.Path(route["file"]).parent / TEST_FILENAME


def test_command(api_dir):
    return ["go", "test", "-run", "TestContractVerify", "-count=1", "./..."]


def build_failed(output):
    return ("build failed" in output or "cannot use" in output
            or "undefined:" in output or "syntax error" in output)


def skipped(output):
    return "--- SKIP" in output or "SKIP:" in output
