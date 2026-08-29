#!/usr/bin/env python3
"""Checks for learned memory: the ledger, retrieval, calibration and rules.

Memory is the one part of this system that could make the auditor worse without
making it look worse. A suppressed claim class produces no counter-evidence, so
precision rises while the misses stop being counted. Most of what is asserted
here is therefore not "does it learn" but "can it still be contradicted":

  * annotation reorders findings and never drops one;
  * a rule is demoted once later evidence argues against it;
  * epsilon exploration samples a real fraction, deterministically, and rotates
    that sample as the ledger grows.

Offline and free: no model call, no gate run.

    python3 auditor/test_memory.py
"""

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "memory"))

import ledger as ledger_mod  # noqa: E402
import rules as rules_mod  # noqa: E402
from recall import Memory  # noqa: E402

results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))


def claim(path, method, kind, detail="", source="agent"):
    return {"path": path, "method": method, "kind": kind, "detail": detail,
            "source": source, "severity": "medium", "evidence": "handlers/x.go:1"}


def rows_for(pairs, repo="acme", run_id="run-1"):
    return [ledger_mod.entry(c, verdict, repo=repo, run_id=run_id) for c, verdict in pairs]


def main():
    # -- shape ------------------------------------------------------------
    a = ledger_mod.shape(claim("/payouts/{id}", "get", "response_field_mismatch", "fee"))
    b = ledger_mod.shape(claim("/payouts/{payoutID}", "get", "response_field_mismatch", "fee"))
    check("path parameters normalise away", a["path"] == b["path"] == "/payouts/{}")
    check("namespace generalises to the first segment", a["namespace"] == "/payouts")
    check("detail prose reduces to an identifier",
          ledger_mod.detail_token("the perPage query parameter default") == "perpage")

    # -- ledger round trip -------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "ledger.jsonl"
        first = rows_for([(claim("/payouts", "post", "validation_mismatch", "amount"), "refuted")])
        ledger_mod.record(first, path)
        ledger_mod.record(rows_for([(claim("/payouts", "post", "status_code_mismatch", "201"),
                                     "confirmed")], run_id="run-2"), path)
        rows = ledger_mod.read(path)
        check("ledger appends rather than replaces", len(rows) == 2, str(len(rows)))
        check("refuted claims are kept, not discarded",
              any(r["verdict"] == "refuted" for r in rows))
        check("refuted rows are labelled as false positives",
              rows[0]["label"] == "false positive", rows[0]["label"])
        check("run count sees both runs", ledger_mod.run_count(rows) == 2)

        with open(path, "a") as f:
            f.write("{not json\n")
        check("a corrupted line degrades the priors, it does not stop the audit",
              len(ledger_mod.read(path)) == 2)

    # -- calibration -------------------------------------------------------
    history = rows_for(
        [(claim("/payouts", "post", "status_code_mismatch", "201"), "confirmed")] * 9
        + [(claim("/payouts", "post", "status_code_mismatch", "201"), "refuted")]
        + [(claim("/refunds", "post", "validation_mismatch", "amount"), "refuted")] * 8
        + [(claim("/refunds", "post", "validation_mismatch", "amount"), "confirmed")] * 2)
    memory = Memory(history)
    table = memory.calibration()
    check("a reliable kind calibrates high", table["status_code_mismatch"]["rate"] > 0.8,
          str(table["status_code_mismatch"]))
    check("an unreliable kind calibrates low", table["validation_mismatch"]["rate"] < 0.35,
          str(table["validation_mismatch"]))
    check("an unseen kind sits at the uninformative prior",
          memory.rate("auth_mismatch") == 0.5, str(memory.rate("auth_mismatch")))
    check("unsupported and error verdicts are not counted as labels",
          table["status_code_mismatch"]["support"] == 10)

    # Budget: the unreliable kind earns more warning, the reliable one earns a
    # gate retry. Both are spending decisions, neither drops a claim.
    check("an unreliable kind earns more retrieved negatives",
          memory.budget("validation_mismatch")["retrieve_k"]
          > memory.budget("status_code_mismatch")["retrieve_k"])
    check("only a well-supported reliable kind earns a gate retry",
          memory.budget("status_code_mismatch")["gate_retries"] == 1
          and memory.budget("validation_mismatch")["gate_retries"] == 0)

    # -- ranking, which must never remove anything -------------------------
    findings = [claim("/refunds", "post", "validation_mismatch", "amount"),
                claim("/payouts", "post", "status_code_mismatch", "201")]
    ranked = memory.annotate(findings)
    check("annotation keeps every finding", len(ranked) == len(findings))
    check("the better-calibrated kind ranks first",
          ranked[0]["kind"] == "status_code_mismatch", ranked[0]["kind"])
    check("the prior behind the ranking is written into the finding",
          ranked[0]["prior"]["support"] == 10 and "confirm_rate" in ranked[0]["prior"])

    # -- retrieval ---------------------------------------------------------
    negatives = memory.negatives("/refunds", "post", {"validation_mismatch"}, k=3)
    check("retrieval returns refuted claims only",
          negatives and all(n["verdict"] == "refuted" for n in negatives))
    check("retrieval stays inside the endpoint's vocabulary",
          all(n["kind"] == "validation_mismatch" for n in negatives))
    check("an out-of-vocabulary kind retrieves nothing",
          memory.negatives("/refunds", "post", {"auth_mismatch"}) == [])

    same_endpoint = Memory(rows_for(
        [(claim("/refunds", "post", "validation_mismatch", "amount"), "refuted"),
         (claim("/other", "get", "validation_mismatch", "amount"), "refuted")]))
    top = same_endpoint.negatives("/refunds", "post", {"validation_mismatch"}, k=1)
    check("the same endpoint outranks the same kind elsewhere",
          top[0]["endpoint"] == "POST /refunds", top[0]["endpoint"])

    block = memory.prompt_block("/refunds", "post", {"validation_mismatch"})
    check("the recalled block reaches the prompt as evidence", "refuted" in block)
    check("the recalled block does not instruct the model to stay silent",
          "do not report" not in block.lower() and "report it anyway" in block.lower())
    check("no history means no prompt block",
          Memory([]).prompt_block("/refunds", "post", {"validation_mismatch"}) == "")

    # -- rules -------------------------------------------------------------
    dismissed = rows_for([(claim(f"/admin/thing{i}", "get", "route_undocumented"), "refuted")
                          for i in range(4)])
    promoted = rules_mod.promote(dismissed)
    check("four dismissals of one shape promote to a rule", len(promoted) == 1, str(promoted))
    check("a rule carries the count of evidence behind it",
          promoted and promoted[0]["support"] == 4)
    check("two dismissals do not promote", rules_mod.promote(dismissed[:2]) == [])

    reviewed = rules_mod.review(list(promoted), dismissed + rows_for(
        [(claim("/admin/thing9", "get", "route_undocumented"), "confirmed")] * 2))
    check("a rule the evidence turns against is demoted",
          reviewed[0]["status"] == "demoted", str(reviewed[0]))
    kept = rules_mod.review(list(promoted), dismissed + rows_for(
        [(claim("/admin/thing9", "get", "route_undocumented"), "confirmed")]))
    check("one confirmation does not overturn four dismissals",
          kept[0]["status"] == "active" and kept[0]["contradicted"] == 1)
    check("a demoted rule is not loaded into memory",
          Memory([], reviewed).rules == [])

    # -- epsilon exploration -----------------------------------------------
    endpoints = [(f"/thing{i}", "get") for i in range(400)]
    explorer = Memory(history, epsilon=0.05, seed=0)
    sampled = [e for e in endpoints if explorer.explore("acme", *e)]
    check("exploration samples roughly epsilon of endpoints",
          0.01 < len(sampled) / len(endpoints) < 0.12, f"{len(sampled)}/400")
    check("exploration is deterministic within a run",
          sampled == [e for e in endpoints if explorer.explore("acme", *e)])
    rotated = Memory(history, epsilon=0.05, seed=7)
    check("the explored sample rotates as the ledger grows",
          sampled != [e for e in endpoints if rotated.explore("acme", *e)])
    check("an explored endpoint is audited with nothing recalled",
          explorer.without_memory().prompt_block("/refunds", "post",
                                                 {"validation_mismatch"}) == "")
    check("epsilon zero explores nothing",
          not any(Memory(history, epsilon=0.0).explore("acme", *e) for e in endpoints))

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
