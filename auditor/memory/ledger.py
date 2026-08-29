#!/usr/bin/env python3
"""The claim ledger: mechanism 1 of the self-improvement design.

An append-only JSONL record of every claim the auditor has ever made, including
the ones the gate threw away. The discarded half is the valuable half: a refuted
claim is a labelled false positive, produced at runtime, at no extra cost, with
no human in the loop.

This is the only part of the scheme that is expensive to retrofit. Reconstructing
rejected claims from runs that never logged them means paying for every run
again, so it writes from the first version even while nothing reads it.

    from memory.ledger import record, read, shape
    record(entries, repo="hyparrow")
    rows = read()

Nothing here suppresses anything. The ledger observes; the gate still decides.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import time

HERE = pathlib.Path(__file__).resolve().parent
LEDGER = HERE / "ledger.jsonl"

# Verdicts the gate can return, and what each one is worth as training data.
LABELLED = {"confirmed": "true positive", "refuted": "false positive"}
UNLABELLED = {"unsupported": "not executable", "error": "gate failed"}

# A path with its parameters normalised away, so /payouts/{id} and
# /payouts/{payoutID} share a shape. Retrieval keys on shape rather than raw
# text: deterministic, dependency-free, and stable across repositories.
PARAM = re.compile(r"\{[^}]*\}")
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def path_shape(path):
    return PARAM.sub("{}", str(path or "").rstrip("/") or "/")


def namespace(path):
    """The first path segment, which is what a learned rule generalises over."""
    parts = [p for p in str(path or "").split("/") if p and not p.startswith("{")]
    return f"/{parts[0]}" if parts else "/"


# Words that appear in a model's phrasing of a claim and never name the thing
# the claim is about. Without this, "the perPage query parameter default" keys
# on "parameter", and every claim about a query parameter shares a bucket.
PROSE = {"the", "and", "for", "not", "but", "its", "was", "are", "does", "spec",
         "code", "handler", "value", "field", "parameter", "param", "query",
         "default", "required", "request", "response", "body", "header",
         "status", "returns", "documented", "documents", "mark", "marks",
         "specification", "endpoint", "constraint", "must", "with", "that",
         "this", "when", "from", "into", "which"}


def detail_token(detail):
    """The identifier a claim is about, pulled out of whatever prose wraps it.

    A deterministic rule emits a bare field name; the model writes a phrase
    around it. An identifier carrying an underscore or an internal capital is
    taken as the answer outright, since prose does not contain those; otherwise
    the longest word that is not prose wins.
    """
    words = WORD.findall(str(detail or ""))
    identifiers = [w for w in words if len(w) > 2 and ("_" in w or w[1:] != w[1:].lower())]
    if identifiers:
        return identifiers[0].lower()
    words = [w for w in words if len(w) > 2 and w.lower() not in PROSE]
    return max(words, key=len).lower() if words else ""


def shape(claim):
    """The structural fingerprint a claim is retrieved and generalised by."""
    return {
        "kind": claim.get("kind", ""),
        "path": path_shape(claim.get("path")),
        "namespace": namespace(claim.get("path")),
        "method": str(claim.get("method", "")).lower(),
        "detail": detail_token(claim.get("detail")),
        "source": claim.get("source", ""),
    }


def key(claim):
    """A single deterministic string for exact-shape grouping."""
    s = shape(claim)
    return "|".join((s["kind"], s["method"], s["path"], s["detail"]))


def entry(claim, verdict, repo="", run_id="", memory="on", verification=""):
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": run_id,
        "repo": repo,
        "endpoint": f"{str(claim.get('method', '')).upper()} {claim.get('path', '')}",
        "kind": claim.get("kind", ""),
        "verdict": verdict,
        "label": LABELLED.get(verdict, ""),
        "memory": memory,
        "claim": claim.get("detail", ""),
        "evidence": {
            "static": str(claim.get("evidence", ""))[:400],
            "gate": str(verification)[:400],
            "result": "passed" if verdict == "refuted" else
                      ("failed" if verdict == "confirmed" else verdict),
        },
        "confidence": claim.get("confidence", ""),
        "source": claim.get("source", ""),
        "shape": shape(claim),
    }


def record(entries, path=LEDGER):
    """Append. Never rewrites, never truncates: history that can be edited by a
    later run is history that cannot be used as evidence.

    A ledger that cannot be written - a read-only image, a mounted workspace -
    costs the next run its priors and nothing else. It must never take down the
    audit that produced it, so the failure is reported and swallowed.
    """
    if not entries:
        return 0
    path = pathlib.Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            for row in entries:
                f.write(json.dumps(row, default=str) + "\n")
    except OSError as exc:
        print(f"::warning::could not write the claim ledger at {path}: {exc}", flush=True)
        return 0
    return len(entries)


def read(path=LEDGER):
    """Every row, oldest first. A malformed line is skipped rather than fatal:
    a corrupted ledger must degrade the priors, not stop the audit."""
    path = pathlib.Path(path)
    if not path.exists():
        return []
    try:
        text = path.read_text()
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def run_count(rows):
    """How many runs the ledger has seen. Used as the exploration seed, so the
    endpoints sampled without memory rotate between runs and stay reproducible."""
    return len({r.get("run_id") for r in rows if r.get("run_id")})


def new_run_id():
    return time.strftime("run-%Y%m%dT%H%M%S", time.gmtime())


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ledger", default=str(LEDGER))
    parser.add_argument("--kind", help="show only rows of this kind")
    parser.add_argument("--verdict", help="show only rows with this verdict")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    rows = read(args.ledger)
    if args.kind:
        rows = [r for r in rows if r.get("kind") == args.kind]
    if args.verdict:
        rows = [r for r in rows if r.get("verdict") == args.verdict]

    if not rows:
        print(f"ledger {args.ledger}: empty")
        return

    counts = {}
    for row in rows:
        counts[row.get("verdict", "?")] = counts.get(row.get("verdict", "?"), 0) + 1
    print(f"{len(rows)} claim(s) over {run_count(rows)} run(s): "
          + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    print()
    for row in rows[-args.limit:]:
        print(f"  {row['ts']}  {row['verdict']:<11} {row['endpoint']:<28} "
              f"{row['kind']:<28} {str(row.get('claim'))[:40]}")


if __name__ == "__main__":
    main()
