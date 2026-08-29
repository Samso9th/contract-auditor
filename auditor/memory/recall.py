#!/usr/bin/env python3
"""Retrieval and calibration: mechanisms 2 and 4, plus epsilon exploration.

Three things read the ledger, and none of them can drop a claim.

**Retrieval (mechanism 2).** Before auditing an endpoint, pull the most similar
previously *refuted* claims and put them in the prompt: these looked like drift,
and here is the test that showed they were not. Keyed on structural shape rather
than raw text, which keeps it deterministic and dependency-free; embeddings can
come later if the structural keys prove too coarse, and they would need a vector
store this project does not otherwise want.

**Calibration (mechanism 4).** The survival rate of each claim kind through the
gate is arithmetic, not learning. If `status_code_mismatch` survives 90% of the
time and `validation_mismatch` 20%, that ratio should drive two things: where a
finding sits in the ranking, and how much verification budget the claim is worth.
No model, no training, and completely explainable to whoever has to trust the
output.

**Epsilon exploration.** The failure mode this whole design has to survive is
feedback poisoning: warn the model away from a class, it stops claiming that
class, the absence of claims is read as the warning being right, and the belief
becomes unfalsifiable. So a small random fraction of endpoints is audited with
memory switched off entirely, purely to keep measuring the classes memory has
learned to distrust. It ships with the first suppression, never after it.

    from memory.recall import Memory
    memory = Memory.load()
    memory.prompt_block("/payouts", "post", kinds)
    memory.priority(finding)
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import zlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ledger as ledger_mod  # noqa: E402
import rules as rules_mod  # noqa: E402

# Fraction of endpoints audited with memory disabled. Small enough that the
# precision gain from retrieval survives it, large enough that a wrong rule is
# contradicted within a few runs rather than never.
EPSILON = 0.05

SEVERITY_WEIGHT = {"high": 3.0, "medium": 2.0, "low": 1.0}

# How many refuted precedents to show, by how badly the kind performs. A kind
# that survives the gate reliably needs no warning; a kind that mostly does not
# is where the reading budget belongs.
MIN_K, MAX_K = 1, 5


def similarity(row, path, method, kinds):
    """How relevant one past claim is to the endpoint about to be audited.

    Ordering, not probability. Weights are chosen so an exact repeat on the same
    endpoint always outranks a same-kind claim elsewhere, and a claim of a kind
    outside this endpoint's vocabulary never appears at all.
    """
    shape = row.get("shape") or {}
    if kinds and shape.get("kind") not in kinds:
        return 0.0
    score = 3.0  # in-vocabulary kind
    if shape.get("path") == ledger_mod.path_shape(path):
        score += 3.0
    elif shape.get("namespace") == ledger_mod.namespace(path):
        score += 1.0
    if shape.get("method") == str(method).lower():
        score += 1.0
    return score


class Memory:
    """Everything learned so far, in a form the audit can consult cheaply."""

    def __init__(self, rows=(), rules=(), epsilon=EPSILON, seed=0):
        self.rows = list(rows)
        self.rules = [r for r in rules if r.get("status") != "demoted"]
        self.epsilon = epsilon
        self.seed = seed
        self._calibration = None

    @classmethod
    def load(cls, ledger_path=None, rules_path=None, epsilon=EPSILON):
        rows = ledger_mod.read(ledger_path or ledger_mod.LEDGER)
        return cls(rows, rules_mod.load(rules_path or rules_mod.RULES),
                   epsilon=epsilon, seed=ledger_mod.run_count(rows))

    # -- mechanism 4: calibration ------------------------------------------

    def calibration(self):
        """Per kind: how often a claim of that kind survived the gate.

        Laplace-smoothed, so a kind seen twice does not read as a certainty and
        an unseen kind sits at 0.5 - which is exactly the prior "we do not know".
        """
        if self._calibration is None:
            table = {}
            for row in self.rows:
                if row.get("verdict") not in ("confirmed", "refuted"):
                    continue
                bucket = table.setdefault(row.get("kind", ""), {"confirmed": 0, "refuted": 0})
                bucket[row["verdict"]] += 1
            for kind, bucket in table.items():
                total = bucket["confirmed"] + bucket["refuted"]
                bucket["support"] = total
                bucket["rate"] = round((bucket["confirmed"] + 1) / (total + 2), 3)
            self._calibration = table
        return self._calibration

    def rate(self, kind):
        return self.calibration().get(kind, {}).get("rate", 0.5)

    def support(self, kind):
        return self.calibration().get(kind, {}).get("support", 0)

    def budget(self, kind):
        """What a claim of this kind is worth spending on.

        Two dials, both driven by the survival rate. A kind that is usually
        wrong earns more retrieved precedents in the prompt - the cheap
        intervention that stops it being claimed in the first place. A kind that
        is usually right earns a retry when the gate fails to run, because there
        a gate error is much more likely to be losing a real finding.
        """
        rate = self.rate(kind)
        k = MIN_K + round((1.0 - rate) * (MAX_K - MIN_K))
        return {"retrieve_k": int(max(MIN_K, min(MAX_K, k))),
                "gate_retries": 1 if (rate >= 0.5 and self.support(kind) >= 3) else 0,
                "rate": rate, "support": self.support(kind)}

    def priority(self, finding):
        """Rank within a severity band by how often the kind has held up."""
        weight = SEVERITY_WEIGHT.get(finding.get("severity"), 1.0)
        return round(weight * self.rate(finding.get("kind", "")), 4)

    def annotate(self, findings):
        """Attach the prior to each finding and order by it.

        The prior is written into the finding rather than applied invisibly, so
        a reviewer can see that a finding ranks where it does because claims of
        its kind have survived the gate 9 times in 10 - and can disagree.
        """
        out = []
        for finding in findings:
            kind = finding.get("kind", "")
            enriched = dict(finding)
            if self.support(kind):
                enriched["prior"] = {"confirm_rate": self.rate(kind),
                                     "support": self.support(kind)}
            enriched["priority"] = self.priority(finding)
            out.append(enriched)
        out.sort(key=lambda f: (-f["priority"], f.get("path", ""), f.get("kind", "")))
        return out

    # -- mechanism 2: retrieval --------------------------------------------

    def negatives(self, path, method, kinds, k=3):
        """The k most similar refuted claims, most similar first."""
        scored = []
        for i, row in enumerate(self.rows):
            if row.get("verdict") != "refuted":
                continue
            score = similarity(row, path, method, kinds)
            if score > 0:
                # `i` breaks ties toward the more recent claim, which is the one
                # more likely to reflect the code as it is now.
                scored.append((score, i, row))
        scored.sort(key=lambda t: (-t[0], -t[1]))
        return [row for _, _, row in scored[:k]]

    def rules_for(self, path, kinds):
        ns = ledger_mod.namespace(path)
        return [r for r in self.rules
                if r.get("namespace") in (ns, "/") and (not kinds or r.get("kind") in kinds)]

    def prompt_block(self, path, method, kinds):
        """The memory section of the prompt, or "" when there is nothing to say.

        Worded as evidence rather than instruction on purpose. "Do not report X"
        teaches the model to stop producing a class of claim, which is precisely
        the poisoning this design is built to avoid; "here is the test that
        refuted this before" leaves the judgment where it belongs.
        """
        k = max(self.budget(kind)["retrieve_k"] for kind in kinds) if kinds else 3
        past = self.negatives(path, method, kinds, k=k)
        rules = self.rules_for(path, kinds)
        if not past and not rules:
            return ""

        lines = ["# What earlier runs learned about claims like these", "",
                 "This is evidence from previous audits, not an instruction. If the "
                 "handler and the spec contradict each other now, report it anyway - "
                 "a test will settle it either way."]
        if past:
            lines += ["", "Claims of these kinds were made before and a generated test "
                          "proved the code was already honouring the spec:"]
            for row in past:
                lines.append(f'  - {row.get("kind")} on {row.get("endpoint")}: '
                             f'"{str(row.get("claim"))[:90]}" - refuted, '
                             f'{str((row.get("evidence") or {}).get("gate"))[:110]}')
        if rules:
            lines += ["", "Patterns a reviewer or the gate has dismissed repeatedly:"]
            for rule in rules:
                lines.append(f'  - {rule["rule"]} (dismissed {rule["support"]} times)')
        return "\n".join(lines)

    # -- epsilon exploration -----------------------------------------------

    def explore(self, repo, path, method):
        """Is this endpoint in the memory-disabled sample this run?

        Deterministic given the ledger, so a run is reproducible, and seeded on
        the number of past runs, so the sampled endpoints rotate instead of the
        same 5% being explored forever.
        """
        if self.epsilon <= 0:
            return False
        token = f"{self.seed}:{repo}:{str(method).lower()} {path}".encode()
        return (zlib.crc32(token) % 1000) < int(self.epsilon * 1000)

    def without_memory(self):
        """This memory with retrieval switched off, for an explored endpoint."""
        return Memory([], [], epsilon=0.0, seed=self.seed)

    def stats(self):
        verdicts = {}
        for row in self.rows:
            verdicts[row.get("verdict", "?")] = verdicts.get(row.get("verdict", "?"), 0) + 1
        return {"claims": len(self.rows), "runs": ledger_mod.run_count(self.rows),
                "verdicts": verdicts, "rules": len(self.rules),
                "refuted_available": verdicts.get("refuted", 0)}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ledger", default=str(ledger_mod.LEDGER))
    parser.add_argument("--endpoint", help='e.g. "post /payouts": show what would be recalled')
    args = parser.parse_args()

    memory = Memory.load(args.ledger)
    stats = memory.stats()
    print(f"ledger: {stats['claims']} claim(s) over {stats['runs']} run(s), "
          f"{stats['refuted_available']} refuted and available as negatives, "
          f"{stats['rules']} active rule(s)\n")

    table = memory.calibration()
    if table:
        print(f"{'KIND':<30} {'CONF':>5} {'REFUTED':>8} {'RATE':>6} {'K':>3} {'RETRY':>6}")
        for kind, bucket in sorted(table.items(), key=lambda kv: -kv[1]["rate"]):
            budget = memory.budget(kind)
            print(f"{kind:<30} {bucket['confirmed']:>5} {bucket['refuted']:>8} "
                  f"{bucket['rate']:>6} {budget['retrieve_k']:>3} {budget['gate_retries']:>6}")
    else:
        print("no calibration yet: the ledger holds no gated claims")

    if args.endpoint:
        method, path = args.endpoint.split(None, 1)
        block = memory.prompt_block(path.strip(), method.strip(), None)
        print("\n" + (block or "(nothing recalled for this endpoint)"))


if __name__ == "__main__":
    main()
