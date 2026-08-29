#!/usr/bin/env python3
"""Promoting repeated dismissals into rules: mechanism 3.

The allowlist is per-instance: *this divergence, on this endpoint, is
intentional.* That does not generalise, so a reviewer ends up writing the same
judgment out once per route. When several dismissals share a structural shape,
the judgment behind them is a rule, and a rule can be stated once and counted.

    {"rule": "routes under /admin/* are internal, not undocumented",
     "support": 47, "source": "learned", "first_seen": "2026-08-14"}

Two properties matter more than the promotion itself.

**A rule carries its support count**, so it can be re-evaluated and retired. A
rule you cannot demote is a belief you cannot correct, which is how a blind spot
becomes permanent.

**A rule adjusts priors; it never suppresses a claim.** It changes where a
finding ranks and how much the prompt warns about the shape. If the model still
makes the claim and the gate still fails the test, the drift is real, and last
month's arithmetic does not get a veto over today's evidence.

    python3 auditor/memory/rules.py --promote
    python3 auditor/memory/rules.py --review
"""

from __future__ import annotations

import argparse
import json
import pathlib

import ledger as ledger_mod

HERE = pathlib.Path(__file__).resolve().parent
RULES = HERE / "rules.json"
ALLOWLIST = HERE / "allowlist.json"

# Three is the smallest count that distinguishes a pattern from a coincidence,
# and the threshold the design note names. Raised by --min-support when a
# reviewer wants stronger evidence before a shape earns a rule.
MIN_SUPPORT = 3

PHRASING = {
    "route_undocumented": "routes under {ns}/* are internal, not undocumented",
    "response_field_mismatch": "the {detail} field under {ns}/* is documented but not returned by design",
    "status_code_mismatch": "the success status under {ns}/* differs from the spec by design",
    "request_required_mismatch": "{ns}/* requires {detail} deliberately, though the spec marks it optional",
    "default_value_mismatch": "the documented default for {detail} under {ns}/* is stale, not the handler",
    "validation_mismatch": "the {detail} constraint under {ns}/* is enforced more tightly on purpose",
}


def phrase(shape):
    template = PHRASING.get(shape["kind"],
                            "{kind} claims under {ns}/* have been dismissed before")
    return template.format(ns=shape.get("namespace") or "/",
                           detail=shape.get("detail") or "that field",
                           kind=shape.get("kind"))


def group_key(shape):
    """Generalise one step above the ledger's exact key: kind plus namespace.

    This is what makes a rule a rule. Keeping `detail` in the key would produce
    one rule per field name, which is the allowlist again with extra steps.
    """
    return f"{shape.get('kind', '')}|{shape.get('namespace', '/')}"


def dismissals(rows, accepted):
    """Everything a reviewer or the gate has rejected.

    Refuted claims come from the gate; allowlist entries come from a human. Both
    are statements that a shape looked like drift and was not, so both count
    toward the same support.
    """
    out = [r for r in rows if r.get("verdict") == "refuted"]
    for item in accepted:
        out.append({"ts": item.get("accepted"), "verdict": "allowlisted",
                    "kind": item.get("kind", ""),
                    "shape": ledger_mod.shape(
                        {"kind": item.get("kind", ""), "path": item.get("path", ""),
                         "method": item.get("method", ""), "detail": item.get("detail", "")}),
                    "endpoint": f"{str(item.get('method', '')).upper()} {item.get('path', '')}"})
    return out


def promote(rows, accepted=(), min_support=MIN_SUPPORT):
    """Every shape dismissed `min_support` times or more, as a rule."""
    groups = {}
    for row in dismissals(rows, accepted):
        shape = row.get("shape") or {}
        groups.setdefault(group_key(shape), []).append((shape, row))

    rules = []
    for gkey, members in sorted(groups.items()):
        if len(members) < min_support:
            continue
        shape = members[0][0]
        stamps = sorted(str(m[1].get("ts") or "") for m in members if m[1].get("ts"))
        rules.append({
            "id": gkey,
            "rule": phrase(shape),
            "kind": shape.get("kind", ""),
            "namespace": shape.get("namespace", "/"),
            "support": len(members),
            "contradicted": 0,
            "status": "active",
            "source": "learned",
            "first_seen": (stamps[0] or "")[:10],
            "last_seen": (stamps[-1] or "")[:10],
            "endpoints": sorted({m[1].get("endpoint", "") for m in members})[:8],
        })
    return rules


def review(rules, rows):
    """Demote a rule the evidence has turned against: epsilon exploration.

    A rule earns its place by counting dismissals. If claims of that shape start
    confirming again - which only happens because ~5% of endpoints are audited
    with memory switched off - the count that justified the rule is stale. The
    rule is marked contradicted rather than deleted, so the history of the
    reversal survives.
    """
    reviewed = []
    for rule in rules:
        against = [r for r in rows
                   if r.get("verdict") == "confirmed"
                   and group_key(r.get("shape") or {}) == rule["id"]]
        # One confirmation does not overturn forty-seven dismissals; a rule
        # confirmed as often as it is dismissed is not a rule any more. New
        # dicts rather than edits in place: a review that mutates its input
        # cannot be run twice on the same rules and compared.
        reviewed.append(dict(rule, contradicted=len(against),
                             status="demoted" if len(against) * 2 >= rule["support"]
                                    else "active"))
    return reviewed


def load(path=RULES):
    path = pathlib.Path(path)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text()).get("rules", [])
    except json.JSONDecodeError:
        return []


def save(rules, path=RULES):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "_comment": "Learned from the claim ledger, not hand-written. A rule adjusts "
                    "priors - ranking and prompt warnings - and never suppresses a "
                    "claim: the verification gate still decides. `support` is how many "
                    "dismissals produced it, `contradicted` how many later confirmations "
                    "argue against it, and a rule whose contradictions reach half its "
                    "support is demoted automatically. Regenerate with "
                    "`python3 auditor/memory/rules.py --promote`.",
        "rules": rules,
    }, indent=2) + "\n")
    return path


def accepted_entries(path=ALLOWLIST):
    path = pathlib.Path(path)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text()).get("accepted", [])
    except json.JSONDecodeError:
        return []


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--promote", action="store_true", help="rebuild rules from the ledger")
    parser.add_argument("--review", action="store_true", help="re-check rules against later evidence")
    parser.add_argument("--min-support", type=int, default=MIN_SUPPORT)
    parser.add_argument("--ledger", default=str(ledger_mod.LEDGER))
    parser.add_argument("--rules", default=str(RULES))
    args = parser.parse_args()

    rows = ledger_mod.read(args.ledger)
    rules = promote(rows, accepted_entries(), args.min_support) if args.promote else load(args.rules)
    if args.promote or args.review:
        rules = review(rules, rows)
        save(rules, args.rules)

    if not rules:
        print(f"no rules yet: no shape has been dismissed {args.min_support} times "
              f"({len(rows)} claim(s) in the ledger)")
        return
    for rule in rules:
        print(f"  [{rule['status']:<8}] support {rule['support']:>3}  "
              f"contradicted {rule['contradicted']:>2}  {rule['rule']}")
        print(f"              {', '.join(rule['endpoints'][:4])}")


if __name__ == "__main__":
    main()
