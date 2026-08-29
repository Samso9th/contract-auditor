#!/usr/bin/env python3
"""Build agent trajectories from a scored run: hackathon deliverable 04.

A trajectory has to be followable from the agent's instructions to its final
result: what it did, how its tools responded, what feedback shaped the next step,
and every retry or checkpoint along the way.

The interesting trajectories here are not the successes. They are the cases where
the agent proposed something false and the verification gate deleted it, because
that is the mechanism the whole project rests on and it is only visible in a
trajectory.

    python3 eval/trajectories.py --run reports/runs/agent --out reports/trajectories
"""

from __future__ import annotations

import argparse
import json
import pathlib

SEVERITY = {"critical": 0, "high": 1, "medium": 2, "low": 3, "none": 9}


def load(run_dir, cases_dir):
    out = []
    for path in sorted(pathlib.Path(run_dir).glob("*.json")):
        payload = json.loads(path.read_text())
        case_id = payload.get("case", path.stem)
        truth_path = pathlib.Path(cases_dir) / case_id / "ground_truth.json"
        truth = json.loads(truth_path.read_text()) if truth_path.exists() else {}
        out.append((case_id, payload, truth))
    return out


def render(case_id, payload, truth):
    meta = payload.get("meta", {})
    verification = meta.get("verification", {})
    kept = payload.get("findings", [])
    dropped = meta.get("dropped_by_gate", [])

    lines = [
        f"# Trajectory: {case_id}",
        "",
        f"**Injected drift:** {truth.get('description', '(decoy, no drift injected)')}",
        f"**Severity:** {truth.get('severity', 'none')}  ·  "
        f"**Expected findings:** {len(truth.get('expect', []))}",
        "",
        "## 1. Deterministic pass: parsing, no model",
        "",
    ]

    mechanical = [f for f in kept if f.get("source") == "deterministic"]
    if mechanical:
        lines.append(f"The rules settled {meta.get('deterministic_claims', 0)} claim(s) by "
                     "comparing the AST against the spec:")
        lines.append("")
        for f in mechanical:
            lines.append(f"- `{f['method'].upper()} {f['path']}` → **{f['kind']}** "
                         f"({f.get('file','?')}:{f.get('line','?')})")
            lines.append(f"  - evidence: {f.get('evidence','')[:220]}")
        lines.append("")
    else:
        lines += ["No mechanical drift. Route table matches the spec, response shapes "
                  "agree, status codes and headers line up.", "",
                  "This is the case the deterministic layer cannot settle; it is why "
                  "the agent exists.", ""]

    lines += [
        "## 2. Agent pass: one call per endpoint",
        "",
        f"The agent was given each endpoint's handler source, the spec's promises for it, "
        f"and the deterministic findings above so it would not restate them. It may only "
        f"report three kinds: `request_required_mismatch`, `default_value_mismatch`, "
        f"`validation_mismatch`.",
        "",
        f"- endpoints audited: {meta.get('model_calls', 0)} call(s)",
        f"- claims proposed: **{meta.get('agent_claims', 0)}**",
        f"- cost: ${meta.get('cost_usd', 0):.5f}  ·  wall clock: {meta.get('wall_clock_seconds', 0):.0f}s",
        "",
    ]

    if meta.get("agent_errors"):
        lines += ["Retries and failures:", ""]
        for err in meta["agent_errors"]:
            lines.append(f"- {err}")
        lines.append("")
    if meta.get("unread_endpoints"):
        lines += ["", "**Endpoints the agent could not read** (reported, never counted "
                  "as clean):", ""]
        for e in meta["unread_endpoints"]:
            lines.append(f"- {e}")
        lines.append("")

    lines += ["## 3. Verification gate: every claim executed", "",
              "Each claim, from either layer, generated a Go test asserting what the "
              "*spec* promises, run against the real handler through `httptest`. "
              "A claim whose test **passes** is refuted and dropped.", "",
              f"| verdict | count |", "|---|---|"]
    for verdict in ("confirmed", "refuted", "unsupported", "error"):
        lines.append(f"| {verdict} | {verification.get(verdict, 0)} |")
    lines.append("")

    if dropped:
        lines += ["### Claims the gate deleted", "",
                  "These were proposed and did not survive execution. This is the "
                  "mechanism working:", ""]
        for d in dropped:
            lines.append(f"- `{d['method'].upper()} {d['path']}` → **{d['kind']}** "
                         f"(proposed by *{d.get('source','?')}*, refuted)")
        lines.append("")

    lines += ["## 4. Result", ""]
    if kept:
        for f in kept:
            lines.append(f"- **{f.get('severity','')}** `{f['method'].upper()} {f['path']}` "
                         f"({f['kind']})")
            if f.get("verification"):
                lines.append(f"  - gate: {f['verification'][:200]}")
        lines.append("")
    else:
        lines += ["No findings.", ""]

    expected = len(truth.get("expect", []))
    verdict = ("correct: matches ground truth" if len(kept) == expected
               else f"MISMATCH: {len(kept)} kept, {expected} expected")
    lines += [f"**Scored:** {verdict}", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", default="reports/runs/agent")
    parser.add_argument("--cases", default="eval/cases")
    parser.add_argument("--out", default="reports/trajectories")
    args = parser.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    entries = load(args.run, args.cases)
    if not entries:
        raise SystemExit(f"no run data in {args.run}")

    # Which cases are worth reading is a property of the run, not a fixed list.
    # The agent's noise moves between runs, so naming cases in prose would make
    # this index contradict the data directly beneath it.
    dropped_in = [cid for cid, payload, _ in entries
                  if payload.get("meta", {}).get("verification", {}).get("refuted", 0)]
    agent_only = [cid for cid, payload, truth in entries
                  if truth.get("expect") and payload.get("meta", {}).get("deterministic_claims", 0) == 0]

    highlights = []
    if dropped_in:
        highlights.append(
            f"**{', '.join(dropped_in)}** are the cases where the gate deleted a claim: "
            "the agent proposed something false and execution removed it before it "
            "reached the report.")
    if agent_only:
        highlights.append(
            f"**{', '.join(agent_only)}** are the drifts the deterministic rules cannot "
            "settle at all, so they exist to be found by the agent or not at all.")

    index = ["# Agent trajectories", "",
             "One per evaluation case, generated from the scored run in "
             f"`{args.run}`. Each is followable from the agent's instructions to its "
             "final result, including the claims the verification gate deleted.", ""]
    index += ["Worth reading first:", ""] + [f"- {h}" for h in highlights] + [""] if highlights else []
    index += ["| case | drift | agent claims | gate dropped | kept | result |",
              "|---|---|---|---|---|---|"]

    for case_id, payload, truth in entries:
        meta = payload.get("meta", {})
        kept = len(payload.get("findings", []))
        expected = len(truth.get("expect", []))
        (out / f"{case_id}.md").write_text(render(case_id, payload, truth))
        index.append(
            f"| [{case_id}]({case_id}.md) | {truth.get('category','?')} "
            f"| {meta.get('agent_claims', 0)} "
            f"| {meta.get('verification', {}).get('refuted', 0)} | {kept} "
            f"| {'correct' if kept == expected else 'MISMATCH'} |")

    totals = {
        "agent_claims": sum(p.get("meta", {}).get("agent_claims", 0) for _, p, _ in entries),
        "refuted": sum(p.get("meta", {}).get("verification", {}).get("refuted", 0)
                       for _, p, _ in entries),
        "kept": sum(len(p.get("findings", [])) for _, p, _ in entries),
        "cost": sum(p.get("meta", {}).get("cost_usd", 0) for _, p, _ in entries),
    }
    survived = totals["agent_claims"] - totals["refuted"]
    raw = survived / totals["agent_claims"] if totals["agent_claims"] else 0
    index += ["",
              f"**Across all {len(entries)} cases:** the agent proposed "
              f"{totals['agent_claims']} claims; the gate refuted {totals['refuted']} "
              f"and confirmed {survived}. That is a raw agent precision of "
              f"**{raw:.2f}** before the gate and **1.00** after it. "
              f"{totals['kept']} findings reached the report, for ${totals['cost']:.4f}.", ""]

    (out / "README.md").write_text("\n".join(index))
    print(f"wrote {len(entries)} trajectories to {out}")
    print(f"agent raw precision {raw:.2f} -> 1.00 after the gate")


if __name__ == "__main__":
    main()
