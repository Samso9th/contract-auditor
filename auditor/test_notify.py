#!/usr/bin/env python3
"""Verify the notification layer without sending anything.

Every check here is about the two ways an alerting system fails in practice: it
sends something wrong, or it sends so much that people stop reading it. Nothing
in this file touches the network.

    python3 auditor/test_notify.py
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from notify import (MAX_LISTED, TELEGRAM_LIMIT, collect, escape_html,  # noqa: E402
                    format_slack, format_telegram, gate_ran, select,
                    summary_line)

ROOT = pathlib.Path(__file__).resolve().parents[1]

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))


def f(severity="high", kind="response_field_mismatch", path="/payouts",
      method="post", case="D01", **extra):
    return {"path": path, "method": method, "kind": kind, "detail": "fee",
            "severity": severity, "case": case, **extra}


def main():
    # -- severity filtering ----------------------------------------------
    findings = [f(severity=s, case=s) for s in ("critical", "high", "medium", "low")]

    check("filter: default keeps everything at or above low",
          len(select(findings, "low")) == 4)
    check("filter: high excludes medium and low",
          {x["severity"] for x in select(findings, "high")} == {"critical", "high"})
    check("filter: critical keeps only critical",
          [x["severity"] for x in select(findings, "critical")] == ["critical"])
    check("filter: unknown severity is dropped, not crashed on",
          len(select(findings + [f(severity="bogus")], "low")) == 4)
    check("filter: worst severity sorts first",
          [x["severity"] for x in select(findings, "low")][0] == "critical")

    # -- the verification gate decides what gets sent ---------------------
    gated = [f(case="a", verdict="confirmed"), f(case="b", verdict="refuted"),
             f(case="c", verdict="unsupported")]
    check("gate: a run carrying verdicts is detected", gate_ran(gated))
    check("gate: a run without verdicts is detected as ungated",
          not gate_ran([f(), f()]))
    check("gate: only confirmed claims survive by default",
          [x["case"] for x in select(gated, "low")] == ["a"])
    check("gate: --unverified sends everything",
          len(select(gated, "low", verified_only=False)) == 3)
    check("gate: ungated runs are not silently emptied",
          len(select([f(), f()], "low")) == 2)

    # -- summary counting -------------------------------------------------
    check("summary: counts every level present",
          summary_line(findings) == "1 critical, 1 high, 1 medium, 1 low")
    check("summary: empty is stated, not blank", summary_line([]) == "no findings")

    # -- slack payload ----------------------------------------------------
    payload = format_slack(select(findings, "low"), "agent")
    check("slack: has a top-level text fallback for notifications",
          isinstance(payload.get("text"), str) and payload["text"])
    check("slack: every block is well formed",
          all("type" in b for b in payload["blocks"]))
    check("slack: header names the run", "agent" in payload["blocks"][0]["text"]["text"])
    check("slack: finding detail reaches the message",
          any("response_field_mismatch" in json.dumps(b) for b in payload["blocks"]))
    check("slack: verified runs claim proof",
          "proved by a test" in json.dumps(payload["blocks"]))
    check("slack: unverified runs carry a warning instead",
          "not been through the verification gate"
          in json.dumps(format_slack(findings, "agent", verified_only=False)["blocks"]))
    check("slack: report url becomes a link",
          "https://ci.example/run/9" in
          json.dumps(format_slack(findings, "agent", "https://ci.example/run/9")["blocks"]))

    many = [f(case=f"D{i:02d}") for i in range(40)]
    big = format_slack(many, "agent")
    check("slack: long runs are capped well under the 50-block limit",
          len(big["blocks"]) <= MAX_LISTED + 4)
    check("slack: the truncated tail is stated, not dropped silently",
          "+30 more" in json.dumps(big["blocks"]))

    # -- telegram payload -------------------------------------------------
    tg = format_telegram(select(findings, "low"), "agent", "-100123")
    check("telegram: chat id is carried", tg["chat_id"] == "-100123")
    check("telegram: html mode is declared", tg["parse_mode"] == "HTML")
    check("telegram: finding detail reaches the message",
          "response_field_mismatch" in tg["text"])

    hostile = format_telegram([f(path="/a?b=<script>&c")], "run <b>", "1")
    check("telegram: angle brackets in a path are escaped",
          "&lt;script&gt;" in hostile["text"] and "<script>" not in hostile["text"])
    check("telegram: ampersands are escaped", "&amp;c" in hostile["text"])
    check("telegram: the title is escaped too", "run &lt;b&gt;" in hostile["text"])
    check("escape: ordering does not double-encode",
          escape_html("&<") == "&amp;&lt;")

    # Paths long enough that even the capped ten findings overrun 4096 - the
    # cap alone does not guarantee the limit, which is why both exist.
    huge = format_telegram([f(path="/" + "x" * 900, case=f"D{i}") for i in range(60)],
                           "agent", "1")
    check("telegram: message stays under the 4096 character limit",
          len(huge["text"]) <= TELEGRAM_LIMIT)
    check("telegram: over-long messages are actually truncated",
          huge["text"].endswith("truncated"))
    check("telegram: truncation lands on a line boundary, leaving no split tag",
          huge["text"].count("<b>") == huge["text"].count("</b>")
          and huge["text"].count("<code>") == huge["text"].count("</code>"))

    # -- reading a real run directory --------------------------------------
    oracle = ROOT / "reports" / "runs" / "oracle"
    if oracle.is_dir():
        loaded = collect(oracle)
        # 15 findings across 12 drifted cases: D01, D05 and D08 each carry two.
        check("collect: reads every finding in a run directory", len(loaded) == 15)
        check("collect: each finding remembers its case",
              all(x.get("case") for x in loaded))
        check("collect: a single file works as well as a directory",
              len(collect(oracle / "D05.json")) == 2)
        check("collect: decoy cases contribute nothing",
              not any(x["case"].startswith("N") for x in loaded))
    else:
        check("collect: oracle run present", False, "run `make check` first")

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
