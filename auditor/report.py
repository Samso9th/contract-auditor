#!/usr/bin/env python3
"""Report emitters: SARIF, webhook, Markdown, and the exit-code policy.

Findings are only useful where the reader already is. Three channels, in
descending order of how little the consumer has to set up:

  SARIF     GitHub ingests it natively and renders each finding as an
            annotation on the exact line of the pull request diff. Nothing to
            host, nothing to configure.
  Markdown  A pull request comment for the summary view.
  Webhook   One POST for Postgres, Slack or Telegram to pick up.

    python3 auditor/report.py reports/runs/agent --sarif out.sarif --markdown out.md
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# SARIF has three levels; map onto them rather than inventing a scale GitHub
# will not render.
SARIF_LEVEL = {"critical": "error", "high": "error", "medium": "warning", "low": "note"}

RULE_HELP = {
    "route_missing_from_spec": "An endpoint is registered in code but absent from the published specification. Integrators cannot discover it, and it is not covered by any contract test.",
    "route_missing_from_code": "The specification documents an endpoint that no route serves. Integrators will build against it and receive 404s.",
    "response_field_mismatch": "The response body does not carry a field the specification promises, or carries one it does not document.",
    "response_type_mismatch": "A response field's JSON type differs from the documented type. On monetary fields this silently corrupts value rather than failing loudly.",
    "response_header_mismatch": "A response header named in the documentation is not the header the handler sets. Signature verification against the documented name will reject every delivery.",
    "request_param_mismatch": "A request parameter the handler reads is not the one documented. The documented parameter is silently ignored.",
    "request_required_mismatch": "The handler rejects a request the specification says is valid, by requiring something the spec marks optional.",
    "status_code_mismatch": "The handler's success status differs from the documented one. Clients branching on status break.",
    "undocumented_status": "The handler can return a status the specification does not document, leaving clients without retry or error guidance.",
    "auth_mismatch": "The handler enforces authentication on an endpoint the specification documents as public.",
    "validation_mismatch": "The handler enforces a different bound or format than the documented constraint.",
    "default_value_mismatch": "The handler applies a different default than the documented one when a parameter is omitted.",
}


def load_findings(run_dir):
    """Collect findings from a run directory, or a single report.json."""
    run = pathlib.Path(run_dir)
    findings, meta = [], {"cost_usd": 0.0, "cases": 0, "model_calls": 0}

    if run.is_file():
        payload = json.loads(run.read_text())
        return payload.get("findings", []), payload.get("meta", meta)

    for path in sorted(run.glob("*.json")):
        payload = json.loads(path.read_text())
        for finding in payload.get("findings", []):
            findings.append(dict(finding, case=payload.get("case", path.stem)))
        case_meta = payload.get("meta", {})
        meta["cost_usd"] += case_meta.get("cost_usd", 0.0)
        meta["model_calls"] += case_meta.get("model_calls", 0)
        meta["cases"] += 1

    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.get("severity"), 9),
                                 f.get("path", ""), f.get("method", "")))
    return findings, meta


def parse_location(finding, repo_root=""):
    """Pull `file:line` out of a finding's evidence.

    Every deterministic rule cites a location, which is what makes an inline
    annotation possible. A finding without one still reports, anchored to the
    repository root rather than dropped.
    """
    if finding.get("file"):
        return str(finding["file"]).lstrip("./"), int(finding.get("line") or 1)
    evidence = finding.get("evidence", "") or ""
    for token in evidence.replace(",", " ").split():
        if ":" not in token:
            continue
        candidate, _, line = token.rpartition(":")
        if candidate and line.isdigit() and ("/" in candidate or candidate.endswith(".go")
                                             or candidate.endswith(".ts")):
            return candidate.lstrip("./"), int(line)
    return "", 0


def to_sarif(findings, tool_version="0.1.0", repo_subdir=""):
    """SARIF 2.1.0, the format GitHub code scanning ingests."""
    kinds = sorted({f.get("kind", "unknown") for f in findings}) or list(RULE_HELP)
    rules = [{
        "id": kind,
        "name": "".join(part.title() for part in kind.split("_")),
        "shortDescription": {"text": kind.replace("_", " ")},
        "fullDescription": {"text": RULE_HELP.get(kind, kind.replace("_", " "))},
        "help": {"text": RULE_HELP.get(kind, kind.replace("_", " "))},
        "defaultConfiguration": {"level": "warning"},
        "properties": {"tags": ["contract", "api", "openapi"]},
    } for kind in kinds]

    results = []
    for finding in findings:
        path, line = parse_location(finding)
        if repo_subdir and path:
            path = f"{repo_subdir.rstrip('/')}/{path}"
        message = (f"{finding.get('method', '').upper()} {finding.get('path', '')}: "
                   f"{finding.get('evidence') or finding.get('kind', '')}")
        result = {
            "ruleId": finding.get("kind", "unknown"),
            "level": SARIF_LEVEL.get(finding.get("severity"), "warning"),
            "message": {"text": message},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": path or "README.md"},
                    "region": {"startLine": line or 1},
                }
            }],
            "properties": {
                "severity": finding.get("severity", "medium"),
                "endpoint": f"{finding.get('method', '')} {finding.get('path', '')}",
                "source": finding.get("source", ""),
                "verdict": finding.get("verdict", ""),
            },
        }
        results.append(result)

    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "contract-auditor",
                "version": tool_version,
                "informationUri": "https://github.com/Samso9th/contract-auditor",
                "rules": rules,
            }},
            "results": results,
        }],
    }


def to_markdown(findings, meta, title="Contract audit"):
    if not findings:
        return (f"## {title}\n\n"
                "No contract drift found. Code and specification agree on every "
                "endpoint checked.\n")

    counts = {}
    for finding in findings:
        counts[finding.get("severity", "medium")] = counts.get(finding.get("severity", "medium"), 0) + 1
    summary = " · ".join(f"**{counts[s]}** {s}" for s in
                         sorted(counts, key=lambda x: SEVERITY_ORDER.get(x, 9)))

    lines = [f"## {title}", "",
             f"{len(findings)} finding(s): {summary}", "",
             "| Severity | Endpoint | Kind | Evidence |", "|---|---|---|---|"]
    for finding in findings[:50]:
        evidence = (finding.get("evidence", "") or "").replace("|", "\\|")[:160]
        lines.append(f"| {finding.get('severity', '')} "
                     f"| `{finding.get('method', '').upper()} {finding.get('path', '')}` "
                     f"| {finding.get('kind', '')} | {evidence} |")
    if len(findings) > 50:
        lines.append(f"\n_{len(findings) - 50} further finding(s) omitted._")

    verified = sum(1 for f in findings if f.get("verdict") == "confirmed")
    if verified:
        lines += ["", f"{verified} of {len(findings)} finding(s) were confirmed by "
                      "executing a generated test against the handler; claims whose "
                      "test passed were discarded."]
    return "\n".join(lines) + "\n"


def post_webhook(url, payload, secret=""):
    """POST the report. curl for the same reason the model client uses it:
    no dependency to install, and it works wherever the container runs."""
    headers = ["-H", "Content-Type: application/json"]
    if secret:
        headers += ["-H", f"X-Auditor-Token: {secret}"]
    completed = subprocess.run(
        ["curl", "-sS", "-m", "30", "-X", "POST", *headers,
         "-w", "\n%{http_code}", "-d", json.dumps(payload), url],
        capture_output=True, text=True,
    )
    if completed.returncode != 0:
        return False, completed.stderr.strip()
    body = completed.stdout.strip().rsplit("\n", 1)
    status = body[-1] if body else "000"
    return status.startswith("2"), status


def exit_code(findings, fail_on="high"):
    """Blocking a merge on day one is how a tool gets uninstalled, so the
    threshold is the consumer's choice."""
    if fail_on == "none":
        return 0
    threshold = SEVERITY_ORDER.get(fail_on, 1)
    worst = min((SEVERITY_ORDER.get(f.get("severity"), 9) for f in findings), default=9)
    return 1 if worst <= threshold else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run", help="run directory or a report.json")
    parser.add_argument("--sarif", help="write SARIF here")
    parser.add_argument("--markdown", help="write a Markdown summary here")
    parser.add_argument("--webhook", default=os.environ.get("AUDITOR_WEBHOOK_URL", ""))
    parser.add_argument("--webhook-secret", default=os.environ.get("AUDITOR_WEBHOOK_SECRET", ""))
    parser.add_argument("--repo-subdir", default="", help="prefix SARIF paths with this")
    parser.add_argument("--fail-on", default="high",
                        choices=["critical", "high", "medium", "low", "none"])
    parser.add_argument("--title", default="Contract audit")
    args = parser.parse_args()

    findings, meta = load_findings(args.run)

    if args.sarif:
        pathlib.Path(args.sarif).write_text(
            json.dumps(to_sarif(findings, repo_subdir=args.repo_subdir), indent=2))
        print(f"sarif    {args.sarif} ({len(findings)} result(s))")

    markdown = to_markdown(findings, meta, args.title)
    if args.markdown:
        pathlib.Path(args.markdown).write_text(markdown)
        print(f"markdown {args.markdown}")

    if args.webhook:
        ok, status = post_webhook(args.webhook, {
            "tool": "contract-auditor",
            "repository": os.environ.get("GITHUB_REPOSITORY", ""),
            "sha": os.environ.get("GITHUB_SHA", ""),
            "ref": os.environ.get("GITHUB_REF", ""),
            "run_url": (f"{os.environ.get('GITHUB_SERVER_URL', '')}/"
                        f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/"
                        f"{os.environ.get('GITHUB_RUN_ID', '')}"
                        if os.environ.get("GITHUB_RUN_ID") else ""),
            "summary": {
                "total": len(findings),
                "by_severity": {s: sum(1 for f in findings if f.get("severity") == s)
                                for s in SEVERITY_ORDER},
            },
            "findings": findings,
            "markdown": markdown,
        }, args.webhook_secret)
        print(f"webhook  {'delivered' if ok else 'FAILED'} ({status})")

    code = exit_code(findings, args.fail_on)
    print(f"\n{len(findings)} finding(s); fail-on={args.fail_on} -> exit {code}")
    sys.exit(code)


if __name__ == "__main__":
    main()
