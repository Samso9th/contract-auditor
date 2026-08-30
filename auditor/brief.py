#!/usr/bin/env python3
"""Build a fix brief from a run: instructions a coding agent can act on.

Produces three things from one audit, because people consume them differently:

    <name>_brief.md    self-contained. Every finding, its evidence, and the test
                       that proved it, quoted inline. Copy the whole thing into
                       Cursor, Codex or Claude Code and it has what it needs.
    tests/             the generated tests as runnable files.
    <name>_brief.zip   both together, for someone who wants to run them. In CI
                       this is written beside the output directory rather than
                       inside it, because the artifact upload zips that directory
                       and a zip nested in a zip helps nobody.

The brief is written to be pasted, not read aloud. It opens with the rule the
agent must follow, because the one judgement a tool cannot make is which side of
a disagreement is the promise: sometimes the code is wrong, sometimes the
document is, and getting that backwards turns a documentation fix into a breaking
change.

    python3 auditor/brief.py reports/runs/agent --name payments-api
"""

from __future__ import annotations

import argparse
import json
import pathlib
import zipfile

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

HOW_TO_DECIDE = """## Before you change anything

Each finding below is a place where the code and the published specification
disagree. **Both are candidates for being wrong**, and choosing correctly is the
only judgement this tool deliberately leaves to you:

- If the code's behaviour is the intended one, the specification is stale. Update
  the specification, and treat it as a documentation change.
- If the specification describes what was promised to callers, the code has
  drifted. Fix the code, and treat it as a bug.

Getting this backwards turns a documentation edit into a breaking change for
everyone already integrated. When the finding involves money, authentication, or
a webhook signature, assume the specification is the promise unless you have a
specific reason to believe otherwise, and say which you chose and why.

Where a finding carries a test, that test was run against the real handler,
asserts what the specification promises, and currently fails. When it passes, that
finding is fixed.
"""


def load(run_dir):
    run = pathlib.Path(run_dir)
    findings, meta = [], {"cost_usd": 0.0, "cases": 0}
    if run.is_file():
        payload = json.loads(run.read_text())
        return payload.get("findings", []), payload.get("meta", {})
    for path in sorted(run.glob("*.json")):
        payload = json.loads(path.read_text())
        for finding in payload.get("findings", []):
            findings.append(dict(finding, case=payload.get("case", path.stem)))
        meta["cost_usd"] += payload.get("meta", {}).get("cost_usd", 0.0)
        meta["cases"] += 1
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.get("severity"), 9),
                                 f.get("path", ""), f.get("method", "")))
    return findings, meta


def fence_for(filename):
    return {".go": "go", ".mjs": "javascript", ".js": "javascript",
            ".py": "python", ".php": "php"}.get(pathlib.Path(filename).suffix, "")


def what_was_compared(coverage):
    """How much of the specification was checkable at all.

    Without this, no findings in a category reads as agreement. It is often
    absence: a document that says an operation returns "some object" cannot be
    contradicted by any code, so the response rules pass in silence and the
    silence flatters. Saying which promises were checkable is the difference
    between a report that has looked and one that has only not complained.
    """
    if not coverage.get("operations"):
        return []

    operations = coverage["operations"]
    matched = coverage.get("operations_matched", 0)
    success = coverage.get("success_responses", 0)
    typed = coverage.get("success_responses_with_schema", 0)

    lines = ["## What was compared", "",
             f"- {matched} of {operations} documented operation(s) were matched to a "
             f"route in the code; {coverage.get('routes', 0)} route(s) were read."]

    if success:
        if not typed:
            lines.append(
                f"- **None of the {success} documented success response(s) declares a "
                f"schema.** The response-shape and status rules had nothing to compare "
                f"against, so their silence below is absence of a promise, not "
                f"agreement with one.")
        elif typed < success:
            lines.append(
                f"- {typed} of {success} documented success response(s) declare a "
                f"schema. The response rules could only check those; for the other "
                f"{success - typed} the document makes no checkable promise.")
        else:
            lines.append(
                f"- All {success} documented success response(s) declare a schema, so "
                f"the response rules had a promise to check on every one.")

    if not coverage.get("parameters"):
        lines.append("- No parameters are documented anywhere in the specification, so "
                     "the request-parameter rules had nothing to compare.")

    return lines + [""]


def render(findings, name, meta, repo="", sha="", run_url=""):
    verified = [f for f in findings if f.get("verdict") == "confirmed"]
    counts = {}
    for f in findings:
        counts[f.get("severity", "medium")] = counts.get(f.get("severity", "medium"), 0) + 1
    summary = ", ".join(f"{counts[s]} {s}"
                        for s in sorted(counts, key=lambda x: SEVERITY_ORDER.get(x, 9)))

    out = [f"# Fix brief: {name}", ""]
    if repo:
        out.append(f"Repository: `{repo}`" + (f" at `{sha[:8]}`" if sha else ""))
    # Outside GitHub Actions the URL is assembled from unset variables and comes
    # through as "//actions/runs/", which is worse than omitting the line.
    if run_url and "://" in run_url and not run_url.rstrip("/").endswith("runs"):
        out.append(f"Audit run: {run_url}")
    out += ["",
            f"{len(findings)} finding(s) where the code and the published specification "
            f"disagree: {summary}.",
            (f"{len(verified)} of them were proved by executing a test against the real "
             f"handler." if verified else
             "None were executed to prove them: this run had no API key, so findings "
             "come from comparing the code against the specification by parsing. Each "
             "is still a real disagreement, but check it yourself before changing "
             "anything."),
            ""]
    out += what_was_compared(meta.get("coverage") or {})
    out += [HOW_TO_DECIDE, "---", "", "## Findings", ""]

    for index, f in enumerate(findings, 1):
        location = f"{f.get('file')}:{f.get('line')}" if f.get("file") else "location not resolved"
        out += [f"### {index}. {f.get('method', '').upper()} {f.get('path', '')} "
                f"({f.get('severity', '')})", "",
                f"**What disagrees:** {f.get('kind', '').replace('_', ' ')}"
                + (f" on `{f['detail']}`" if f.get("detail") else ""), "",
                f"**Where:** `{location}`", ""]

        if f.get("evidence"):
            out += ["**What was observed:**", "", f"> {f['evidence']}", ""]
        if f.get("verification") and f.get("verdict") == "confirmed":
            out += [f"**Proved by execution:** {f['verification']}", ""]
        if f.get("verdict") and f["verdict"] != "confirmed":
            out += [f"_Not proved by execution ({f['verdict']}). Treat as a lead, "
                    f"not a fact, and confirm it yourself before changing anything._", ""]

        if f.get("test_source"):
            filename = f.get("test_filename", "contract_test")
            out += [f"**The test that proves it** (`tests/{filename}`). It fails now "
                    f"and passes when the finding is fixed:", "",
                    f"```{fence_for(filename)}", f["test_source"].strip(), "```", ""]
        out.append("---")
        out.append("")

    out += ["## When you are done", "",
            "Run the tests in `tests/` against your changes. Each one asserts what the "
            "specification promises, so a passing test means that finding is resolved. "
            "If you changed the specification instead of the code, regenerate the "
            "specification and re-run the audit rather than editing the test.", ""]
    return "\n".join(line for line in out if line is not None)


def write(findings, out_dir, name, meta, repo="", sha="", run_url="", zip_path=None):
    """Write the brief, the test files, and a zip of both.

    zip_path places the archive somewhere other than inside out_dir, which is
    what the action does: out_dir is uploaded as an artifact, and GitHub zips it
    on the way out.
    """
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    brief_path = out_dir / f"{name}_brief.md"
    brief_path.write_text(render(findings, name, meta, repo, sha, run_url))

    tests_dir = out_dir / "tests"
    written = []
    for f in findings:
        if not f.get("test_source"):
            continue
        filename = f.get("test_filename") or "contract_test.txt"
        tests_dir.mkdir(exist_ok=True)
        path = tests_dir / filename
        # Two findings on the same endpoint would otherwise overwrite each other.
        suffix = 1
        while path.exists():
            path = tests_dir / f"{pathlib.Path(filename).stem}_{suffix}{pathlib.Path(filename).suffix}"
            suffix += 1
        path.write_text(f["test_source"])
        written.append(path)

    zip_path = pathlib.Path(zip_path) if zip_path else out_dir / f"{name}_brief.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(brief_path, brief_path.name)
        for path in written:
            archive.write(path, f"tests/{path.name}")

    return brief_path, zip_path, written


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run", help="run directory or a report.json")
    parser.add_argument("--name", default="contract-audit")
    parser.add_argument("--out", default="reports/brief")
    parser.add_argument("--repo", default="")
    parser.add_argument("--sha", default="")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--zip-path", default="",
                        help="write the archive here instead of inside --out")
    args = parser.parse_args()

    findings, meta = load(args.run)
    if not findings:
        print("no findings; no brief written")
        return

    brief, archive, tests = write(findings, args.out, args.name, meta,
                                  args.repo, args.sha, args.run_url,
                                  args.zip_path or None)
    print(f"brief  {brief}  ({len(findings)} findings)")
    print(f"tests  {len(tests)} file(s)")
    print(f"zip    {archive}")


if __name__ == "__main__":
    main()
