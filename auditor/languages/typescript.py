"""TypeScript / JavaScript adapter — Express.

Route extraction runs through the TypeScript compiler's own parser (see
tools/tsroutes/extract.mjs) and emits the same table shape as the Go adapter, so
nothing above this layer changes.

Verification is not implemented yet, and is reported as such. The gate will
declare a TypeScript claim `unsupported` rather than let it through unchecked —
a finding that reaches a report without being executed is exactly what this
project refuses to ship, and that rule does not get relaxed for a new language.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

NAME = "typescript"
DEFAULT_PREFIX = "/api/v1"
EXTRACTOR = pathlib.Path(__file__).resolve().parents[1] / "tools" / "tsroutes" / "extract.mjs"

VERIFICATION_SUPPORTED = False


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
    raise NotImplementedError("TypeScript verification is not implemented")


def test_command(api_dir):
    raise NotImplementedError("TypeScript verification is not implemented")


def build_failed(output):
    return False


def skipped(output):
    return False
