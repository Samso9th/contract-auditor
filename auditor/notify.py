#!/usr/bin/env python3
"""The notification layer: how a finding reaches a human.

A report nobody opens is the same as no report. This module pushes verified
drift to Slack or Telegram the moment CI produces it, so the cost of a broken
contract lands on the person who can still cheaply fix it.

Three rules, and they are the reason this file is small:

**Only verified findings are sent.** If the run went through the verification
gate, anything with a verdict other than `confirmed` stays out of the alert.
Pushing a plausible-but-wrong claim into a team channel is how a tool gets muted,
and a muted tool has negative value - it costs attention and returns nothing.

**Silence is the default.** A clean run sends nothing. Alerts that fire on every
build stop being read within a week; `--notify-empty` exists for the one case
where a green heartbeat is genuinely wanted.

**Transport is curl.** Same measurement that forced it in `llm.py` - this
system Python links LibreSSL 2.8.3 and urllib takes ~160s for what curl does in
seconds. It also keeps the harness dependency-free.

    python3 auditor/notify.py --run reports/runs/agent --dry-run
    python3 auditor/notify.py --run reports/runs/agent --min-severity high

Credentials come from `.env` or the environment:

    SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
    TELEGRAM_BOT_TOKEN=123456:ABC...
    TELEGRAM_CHAT_ID=-1001234567890
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"

# Ordered worst-first. Index is the comparison key everywhere below, so adding a
# level means adding it here and nowhere else.
SEVERITY_ORDER = ["critical", "high", "medium", "low"]

ICON = {"critical": ":rotating_light:", "high": ":red_circle:",
        "medium": ":large_orange_diamond:", "low": ":white_circle:"}

# Slack renders at most 50 blocks and Telegram truncates at 4096 characters. A
# run against a real repository can produce far more findings than that, so the
# alert carries the worst ones and points at the report for the rest. Losing the
# tail of a message is a worse failure than saying "+37 more".
MAX_LISTED = 10
TELEGRAM_LIMIT = 4096


class NotifyError(RuntimeError):
    pass


def load_env(path=ENV_FILE):
    """Read `.env` without exporting it, then let the real environment win.

    Deliberately not shared with `llm.py`'s loader: that one filters to model
    credentials, and a webhook URL is a credential this process should be able
    to read without also demanding an API key.
    """
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("SLACK_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


# -- collecting findings -------------------------------------------------

def collect(target):
    """Flatten a run directory (or a single findings file) into one list.

    Each entry keeps the case or repository it came from, because an alert that
    says which endpoint broke but not which service is not actionable.
    """
    target = pathlib.Path(target)
    files = sorted(target.glob("*.json")) if target.is_dir() else [target]
    if not files:
        raise NotifyError(f"no findings files under {target}")

    findings = []
    for path in files:
        try:
            document = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise NotifyError(f"{path.name}: {exc}") from exc
        label = document.get("case") or document.get("repo") or path.stem
        for item in document.get("findings", []):
            findings.append({**item, "case": label})
    return findings


def gate_ran(findings):
    """True when any finding carries a verdict, i.e. the verification gate ran.

    Used to decide the default for `--verified-only`: if the gate ran, its
    judgement is the whole point and unconfirmed claims are dropped. If it did
    not, everything is sent and the alert says so rather than implying a
    guarantee it cannot make.
    """
    return any("verdict" in f for f in findings)


def select(findings, min_severity="low", verified_only=None):
    if verified_only is None:
        verified_only = gate_ran(findings)

    ceiling = SEVERITY_ORDER.index(min_severity)
    kept = []
    for f in findings:
        severity = f.get("severity", "medium")
        if severity not in SEVERITY_ORDER or SEVERITY_ORDER.index(severity) > ceiling:
            continue
        if verified_only and f.get("verdict", "confirmed") != "confirmed":
            continue
        kept.append(f)

    kept.sort(key=lambda f: (SEVERITY_ORDER.index(f.get("severity", "medium")),
                             f["case"], f["path"], f.get("method", "")))
    return kept


def tally(findings):
    counts = {level: 0 for level in SEVERITY_ORDER}
    for f in findings:
        counts[f.get("severity", "medium")] = counts.get(f.get("severity", "medium"), 0) + 1
    return counts


def summary_line(findings):
    counts = tally(findings)
    parts = [f"{counts[level]} {level}" for level in SEVERITY_ORDER if counts[level]]
    return ", ".join(parts) or "no findings"


def describe(f):
    """One line per finding: what broke, where, and on what evidence."""
    method = f.get("method", "").upper()
    head = f"{method} {f['path']}: {f['kind']}"
    if f.get("detail"):
        head += f" ({f['detail']})"
    return head


# -- formatting ----------------------------------------------------------

def format_slack(findings, title, report_url=None, verified_only=True):
    listed = findings[:MAX_LISTED]
    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"Contract drift: {title}"}},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": f"*{len(findings)} finding(s)*: {summary_line(findings)}"}},
    ]

    for f in listed:
        line = f"{ICON.get(f.get('severity'), '')} *{f.get('severity', 'medium').upper()}*  " \
               f"`{describe(f)}`\n_{f['case']}_"
        if f.get("evidence"):
            line += f" · {f['evidence']}"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": line}})

    if len(findings) > MAX_LISTED:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"_+{len(findings) - MAX_LISTED} more in the full report_"}]})

    footer = ("Every finding above was proved by a test that fails against the current code."
              if verified_only else
              ":warning: Unverified run: these claims have not been through the verification gate.")
    if report_url:
        # The artifact id is only minted after this action has finished, so the
        # message cannot carry a direct download link. Naming the panel is the
        # next best thing: the brief is at the foot of the run summary page.
        footer += f" <{report_url}|Open the run> · the fix brief is under Artifacts."
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})

    return {"blocks": blocks,
            "text": f"Contract drift: {title}, {summary_line(findings)}"}


def escape_html(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_telegram(findings, title, chat_id, report_url=None, verified_only=True):
    lines = [f"<b>Contract drift: {escape_html(title)}</b>",
             f"{len(findings)} finding(s): {escape_html(summary_line(findings))}", ""]

    for f in findings[:MAX_LISTED]:
        lines.append(f"<b>{escape_html(f.get('severity', 'medium').upper())}</b> "
                     f"<code>{escape_html(describe(f))}</code>")
        lines.append(f"<i>{escape_html(f['case'])}</i>")
        lines.append("")

    if len(findings) > MAX_LISTED:
        lines.append(f"+{len(findings) - MAX_LISTED} more in the full report")

    lines.append("Every finding was proved by a failing test." if verified_only
                 else "Unverified run: no verification gate.")
    if report_url:
        lines.append("Open the run, then take the fix brief from the Artifacts "
                     "panel at the foot of the summary page:")
        lines.append(escape_html(report_url))

    text = "\n".join(lines)
    if len(text) > TELEGRAM_LIMIT:
        # Trim on a line boundary so no HTML tag is cut in half; an unclosed tag
        # makes Telegram reject the whole message with a 400.
        text = text[:TELEGRAM_LIMIT - 40].rsplit("\n", 1)[0] + "\n…truncated"

    return {"chat_id": chat_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True}


# -- transport -----------------------------------------------------------

def post(url, payload, timeout=20):
    completed = subprocess.run(
        ["curl", "-sS", "-m", str(timeout), "-X", "POST",
         "-H", "Content-Type: application/json",
         "-w", "\n%{http_code}",
         "-d", json.dumps(payload), url],
        capture_output=True, text=True,
    )
    if completed.returncode != 0:
        raise NotifyError(completed.stderr.strip() or f"curl exited {completed.returncode}")

    body, _, status = completed.stdout.rpartition("\n")
    if not status.strip().startswith("2"):
        raise NotifyError(f"HTTP {status.strip()}: {body[:300]}")
    return body


def send(findings, title, env, report_url=None, verified_only=True, dry_run=False):
    """Post to whichever channels are configured. Returns the channels used.

    A channel with no credentials is skipped silently rather than treated as an
    error: a repository that only wires Slack should not fail because Telegram
    is unset.
    """
    sent = []
    slack_ready = bool(env.get("SLACK_WEBHOOK_URL"))
    telegram_ready = bool(env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"))

    # A dry run renders both payloads whether or not credentials exist. Being
    # able to read the message before wiring a webhook is the point of the flag.
    if slack_ready or dry_run:
        payload = format_slack(findings, title, report_url, verified_only)
        if dry_run:
            print("--- slack ---")
            print(json.dumps(payload, indent=2))
        else:
            post(env["SLACK_WEBHOOK_URL"], payload)
        sent.append("slack" if slack_ready else "slack (unconfigured)")

    if telegram_ready or dry_run:
        payload = format_telegram(findings, title,
                                  env.get("TELEGRAM_CHAT_ID", "<chat id>"),
                                  report_url, verified_only)
        if dry_run:
            print("--- telegram ---")
            print(json.dumps(payload, indent=2))
        else:
            post(f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/sendMessage",
                 payload)
        sent.append("telegram" if telegram_ready else "telegram (unconfigured)")

    return sent


# -- cli -----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", default="reports/runs/agent",
                        help="run directory or a single findings JSON file")
    parser.add_argument("--title", default=None,
                        help="what to call this run in the alert (default: the run name)")
    parser.add_argument("--min-severity", default="high", choices=SEVERITY_ORDER,
                        help="alert only at or above this level (default: high)")
    parser.add_argument("--report-url", default=None,
                        help="link included in the alert, e.g. the CI run")
    parser.add_argument("--unverified", action="store_true",
                        help="send claims that did not pass the verification gate")
    parser.add_argument("--notify-empty", action="store_true",
                        help="send an all-clear when nothing was found")
    parser.add_argument("--fail-on", default=None, choices=SEVERITY_ORDER,
                        help="exit 2 if any finding is at or above this level (for CI)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the payloads instead of sending them")
    args = parser.parse_args()

    try:
        findings = collect(args.run)
    except NotifyError as exc:
        sys.exit(f"notify: {exc}")

    verified_only = gate_ran(findings) and not args.unverified
    selected = select(findings, args.min_severity,
                      verified_only=False if args.unverified else None)
    title = args.title or pathlib.Path(args.run).name

    env = load_env()
    if not args.dry_run and not (env.get("SLACK_WEBHOOK_URL")
                                 or (env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"))):
        sys.exit("notify: no channel configured. Set SLACK_WEBHOOK_URL, or "
                 "TELEGRAM_BOT_TOKEN with TELEGRAM_CHAT_ID, in .env or the environment.")

    if not selected and not args.notify_empty:
        print(f"notify: nothing at or above {args.min_severity}, staying quiet")
        return

    try:
        sent = send(selected, title, env, args.report_url, verified_only, args.dry_run)
    except NotifyError as exc:
        sys.exit(f"notify: send failed: {exc}")

    where = ", ".join(sent) or "nowhere (no channel configured)"
    verb = "would send" if args.dry_run else "sent"
    print(f"notify: {verb} {len(selected)} finding(s) to {where}: {summary_line(selected)}")

    if args.fail_on:
        ceiling = SEVERITY_ORDER.index(args.fail_on)
        if any(SEVERITY_ORDER.index(f.get("severity", "medium")) <= ceiling for f in selected):
            sys.exit(2)


if __name__ == "__main__":
    main()
