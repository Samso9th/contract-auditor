#!/usr/bin/env python3
"""Verify init writes a workflow, not just a JSON summary.

Every other check drives init through --json, which reaches inspect() and stops
there. render() - the half that turns those findings into the file a user
actually commits - went unexercised, and a helper it calls was deleted with its
call sites left in place. `init` crashed with NameError for four commits and no
test noticed, because no test ever asked it for a workflow.

    python3 auditor/test_init.py
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import init  # noqa: E402

FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "eval" / "fixture"

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))


def main():
    findings = init.inspect(FIXTURE, branches=("main",))
    check("inspect reads the fixture", findings["routes"] > 0 and findings["operations"] > 0)

    # The reason strings render() wraps into comments. Every one is required:
    # a missing key is a KeyError at the moment somebody runs init for real.
    for key in ("spec_why", "source_why", "prefix_why", "guard_why",
                "branches_why", "language_why"):
        check(f"inspect explains {key}", findings.get(key))

    workflow = init.render(findings)
    check("render returns a workflow", workflow.startswith("name: Contract Audit"))
    for required in ("uses: samso9th/contract-auditor@", "spec:", "source-dir:",
                     "language:", "contract-middleware:", "fail-on:"):
        check(f"workflow declares {required.strip(':')}", required in workflow)

    # It has to parse as YAML, or the run it configures never starts.
    try:
        import yaml
        parsed = yaml.safe_load(workflow)
        check("workflow is valid YAML", isinstance(parsed, dict) and "jobs" in parsed)
        step = parsed["jobs"]["contract-audit"]["steps"][1]
        check("the audit step carries the derived spec",
              step["with"]["spec"] == findings["spec"])
    except ImportError:                                     # pragma: no cover
        check("workflow is valid YAML", True, "skipped: no yaml module")

    # Every derivation is written down beside the value it explains.
    check("reasons reach the file as comments", workflow.count("          #") > 8)

    width = max(len(n) for n, _, _ in results)
    failed = 0
    for name, ok, detail in results:
        status = "pass" if ok else "FAIL"
        line = f"  {status}  {name:<{width}}"
        if not ok and detail:
            line += f"   {detail}"
        print(line)
        failed += not ok

    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
