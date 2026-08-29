#!/usr/bin/env python3
"""What each language adapter actually supports.

Printed rather than asserted in prose, so the claim cannot drift from the code.
A language counts as supported only once it has its own fixture, its own injected
mutations, and a passing evaluation - shipping a parser and calling that support
would be claiming a contract that was never verified, which is the exact failure
this tool exists to catch.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import languages  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]

SUITES = {
    "go": ("eval/fixture", "eval/mutations/mutations.json"),
    "typescript": ("eval/fixture-ts", "eval/mutations/mutations-ts.json"),
    "python": ("eval/fixture-py", "eval/mutations/mutations-py.json"),
}


def main():
    print(f"{'LANGUAGE':<12} {'EXTRACT':<9} {'GATE':<7} {'FIXTURE':<9} {'CASES':<7} RULES COVER")
    print("-" * 78)
    for name in languages.names():
        adapter = languages.get(name)
        fixture, mutations = SUITES.get(name, ("", ""))

        has_fixture = bool(fixture) and (ROOT / fixture).exists()
        cases = "-"
        if mutations and (ROOT / mutations).exists():
            import json
            entries = json.loads((ROOT / mutations).read_text())["mutations"]
            drift = sum(1 for m in entries if m["expect"])
            cases = f"{len(entries)} ({drift}d)"

        gate = "yes" if getattr(adapter, "VERIFICATION_SUPPORTED", True) else "NO"
        settled = sorted(getattr(adapter, "DETERMINISTIC_KINDS", set()))
        covers = f"{len(settled)} kinds"

        print(f"{name:<12} {'yes':<9} {gate:<7} {'yes' if has_fixture else 'NO':<9} "
              f"{cases:<7} {covers}")

    print()
    print("Rules cover what the deterministic layer settles without a model. The agent")
    print("covers exactly the remainder for that language, and every claim from either")
    print("layer goes through the verification gate before it reaches a report.")
    print()
    for name in languages.names():
        adapter = languages.get(name)
        settled = sorted(getattr(adapter, "DETERMINISTIC_KINDS", set()))
        print(f"  {name}: {', '.join(settled) if settled else '(none)'}")


if __name__ == "__main__":
    main()
