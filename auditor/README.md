# auditor/

The agent. Not yet implemented - this file records the intended shape so the
build order is fixed before any code is written.

## Interface it must satisfy

```
python3 auditor/run.py --cases eval/cases --out reports/runs/agent
python3 auditor/run.py --repo <dir> --spec <openapi.json> [--docs <dir>] --out <dir>
```

Emits one `<CASE_ID>.json` per case in the format
[eval/score.py](../eval/score.py) documents. The output contract is fixed by the
scorer, so baseline and agent stay directly comparable.

## Components, in build order

1. **`tools/routes.py` - AST route extraction. ✅ Built.** Parses the Go source
   via [`goroutes/`](tools/goroutes/) (`go/ast`) and returns the registered route
   table with each handler's swag annotation attached. Deterministic, no model.

   Verified by [`tools/test_routes.py`](tools/test_routes.py) - 30 checks against
   answers known by construction: 10 routes clean, 11 under D03, 9 under D04, and
   the route table provably unchanged under all 4 decoys and all 9 non-routing
   drifts. Handles stdlib `ServeMux` Go 1.22 patterns and gin/echo/chi routers
   with `Group()` prefix resolution.

   ```bash
   python3 auditor/tools/routes.py eval/fixture --strip-prefix /v1
   python3 auditor/tools/test_routes.py
   ```

   Two bugs the fixture caught that review would not have: `.Get()` on
   `http.Header` and `url.Values` was being counted as a `GET` route, and the
   guard that fixed it then silently dropped every gin `group.GET("", h)`
   registration - 50 real routes on the target repo. Neither was visible without
   a case whose answer was known in advance.

2. **`tools/spec.py` - spec index. ✅ Built.** Load OpenAPI into a lookup of
   `(path, method) → {params, request schema, responses, security}`. Also
   deterministic.

3. **`tools/diff.py` - deterministic rules. ✅ Built — nine rules, not just set difference.** Routes in code but not spec, and the
   reverse. This alone should catch D03 and D04 with no model involved, and that
   is the point: it establishes how much of the problem needs an agent at all.

4. **`audit_endpoint.py` - the per-endpoint agent.** Given one endpoint's
   handler body, its annotation, its spec entry and any prose, emit candidate
   findings. This is where judgment is actually required.

5. **`verify.py` - the verification gate. ✅ Built, 29/29 checks.** For each claim, write a Go test that
   asserts the claimed behaviour, run it against the mutated fixture, and keep
   the finding only if the test *fails* in the way the claim predicts. A claim
   whose test passes was wrong. This is the component the project stands on.

6. **`reconcile.py` - merge, dedupe, rank.** Collapse duplicate findings across
   endpoints, rank by severity, apply the allowlist.

7. **`skills/fintech.md` - domain rules.** Money as decimal never float,
   idempotency on money-moving endpoints, auth scope claims matching middleware,
   webhook signature headers. Feeds severity ranking.

8. **`memory/allowlist.json` - accepted divergence.** Intentional drift recorded
   once, not re-reported.

Build 1–3 first and score them. If deterministic diffing alone scores well on
D03/D04, that is the honest baseline the agent has to beat on the other ten,
and it belongs in the changelog as its own row.

---

## Status

Built and verified, no model required:

| Component | Verification |
|---|---|
| `tools/routes.py` + `goroutes/` | `test_routes.py` — 30/30 |
| `tools/spec.py`, `tools/diff.py` | `test_diff.py` — 27/27 |
| `verify.py` | `test_verify.py` — 29/29 |
| `run_deterministic.py` | F1 0.889, precision 1.0, 4/4 decoys, $0.00, 0.2s |

Remaining — `audit_endpoint.py`, `reconcile.py`, `skills/fintech.md`, the
allowlist, `run.py` and `baseline/run.py` — all call a model, so they need
`ANTHROPIC_API_KEY` and the `anthropic` SDK, neither of which is present in this
environment. They are deliberately not written yet: this project's standard is
that nothing counts as built until it is verified, and unrunnable code cannot be.

The agent layer now has a narrow, well-defined job. The deterministic layer
already catches 12 of 15 drifts at precision 1.0. What is left is D06
(undocumented required field), D09 (default-value drift) and D11 (validation
loosened) — the three needing judgment rather than lookup — and the agent must
take them without lowering precision. Its claims go through the same gate.
