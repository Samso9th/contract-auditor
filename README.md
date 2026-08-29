# Contract Auditor

**micro1 Agentic Workflows Hackathon**

An agent that finds where a Go HTTP API's **code**, its **generated OpenAPI spec**,
and its **published documentation** disagree - and proves each finding with a test
that fails before the fix and passes after.

Nothing reaches the report unverified. That constraint is the whole design.

---

## The user and the bottleneck

**Who has this problem.** Backend teams that publish an HTTP API to external
integrators, and the integrators downstream of them. The motivating case is
Hyparrow, a Nigerian payments API: partner fintechs build against its published
docs, and every disagreement between those docs and the running handlers becomes
someone else's production incident.

**What the bottleneck is.** A published API has at least three descriptions of
itself and no mechanism keeping them aligned:

1. the handler code, which is what actually runs;
2. the `swag` annotations above each handler, from which the OpenAPI spec is generated;
3. the prose documentation partners actually read.

Each is edited by different people at different times. Code review catches
whether the code is correct, not whether it still matches what was promised. So
drift accumulates silently and surfaces at the worst possible moment - at
integration time, in someone else's codebase, with no error message that points
at the real cause.

The measurements below come from the motivating repository (a private Go
payments API, ~324 source files):

| Signal | Value | Why it matters |
|---|---|---|
| Routes registered in code | 841 | The real surface |
| Paths in published OpenAPI spec | 170 | What partners can see |
| Routes carrying no `@Router` annotation | 500 of 841 (59%) | Invisible to the generated spec |
| `@Success` annotations typed as `map[string]interface{}` | 153 of 194 (79%) | The spec documents "an object" and nothing more |

The route count is measured, not estimated - produced by
[auditor/tools/routes.py](auditor/tools/routes.py) walking the AST, which is
also the first component of the auditor itself. A grep for route registrations
returns 805 lines, but that number is both too high and too low: it counts calls
that are not registrations, and misses the 50 routes gin registers as
`group.GET("", handler)` where the path comes entirely from the group prefix.
Getting from 805 to 841 is a small illustration of the whole thesis - the
approximate answer and the correct one are not the same answer.

Not all 841 routes are meant to be public - admin and internal endpoints
legitimately stay undocumented, and classifying which is part of the task. But
no human is diffing 170 paths against 801 routes by hand, which is precisely why
the gap has never been measured.

**Why solving it is valuable.** Drift is expensive in a specific, avoidable way:
it is cheap to fix at the commit that introduced it and expensive once a partner
has built against the wrong contract. Catching it in CI moves the cost back to
where it belongs.

---

## What the agent does

```
                    ┌─────────────────────────────────────┐
                    │ deterministic pre-pass              │
   Go source ──────►│  • AST route table extraction       │
   OpenAPI spec ───►│  • spec path/schema index           │
   Docs (.mdx) ────►│  • set difference: code △ spec      │
                    └──────────────┬──────────────────────┘
                                   │ candidate surface + verified deltas
                    ┌──────────────▼──────────────────────┐
                    │per-endpoint auditor agents (fan-out)│
                    │ handler body vs annotation vs prose │
                    └──────────────┬──────────────────────┘
                                   │ claims
                    ┌──────────────▼──────────────────────┐
                    │ VERIFICATION GATE                   │
                    │ write a Go test asserting the claim │
                    │ run it. Test passes ⇒ claim is      │
                    │ wrong ⇒ discard the finding.        │
                    └──────────────┬──────────────────────┘
                                   │ survivors only
                    ┌──────────────▼──────────────────────┐
                    │ reconciler: dedupe, rank, allowlist │
                    └──────────────┬──────────────────────┘
                                   ▼
                    report + failing tests + spec patch
```

The design choices worth defending:

- **Deterministic work stays deterministic.** The route table comes from
  `go/ast`, not from a model reading files. Set differences between the route
  table and the spec are computed, not inferred. The agent is spent on the part
  that genuinely needs judgment - reconciling a handler body against prose.
- **The verification gate is the contribution.** An unverified claim about API
  behaviour is worth close to nothing, because the failure mode of an LLM reading
  handler code is a confident, plausible, wrong finding. Charging every claim the
  price of a failing test converts the report from opinion into evidence.
- **Fan-out is per endpoint, not per file.** Endpoints are the unit the contract
  is expressed in, and they fit in a context window with room for the schema and
  the prose.
- **Memory is an allowlist.** Intentional divergence gets recorded once and stops
  being re-reported, so repeat runs stay signal.

---

## Evaluation

### Two targets, and why

The motivating repository is private and its working tree contains live
credentials, so it cannot be shipped to judges (ground rules 07 and 08). The
evaluation therefore runs against a **synthetic Go payments API** included in
this repo at [eval/fixture](eval/fixture) - ten endpoints, standard library
only, builds offline, with a committed OpenAPI spec that matches it exactly in
the clean state.

Drift is then **injected**, so the ground truth is known exactly rather than
argued about. This is what makes the numbers below checkable by anyone.

The private repository is reported separately as a case study, with findings
quoted as evidence and no credentials included.

### The cases

16 cases, exceeding the brief's target of ten: **12 injected drifts** across the
categories that actually hurt integrators, and **4 decoys** - refactors that
change code without changing the contract. The decoys are what make precision
measurable; without them, an auditor that reports everything scores perfectly.

| ID | Severity | Drift |
|---|---|---|
| D01 | high | Response field renamed in code, spec unchanged |
| D02 | medium | Handler returns 200, spec documents 201 |
| D03 | high | Endpoint exists in code, absent from spec |
| D04 | high | Spec documents an endpoint that is no longer routed |
| D05 | **critical** | Money field changed from decimal string to `float64` |
| D06 | high | Code requires a field the spec marks optional |
| D07 | high | Query param read as `per_page`, documented as `perPage` |
| D08 | **critical** | Endpoint now requires auth, documented as public |
| D09 | low | Pagination default changed, docs stale |
| D10 | **critical** | Webhook signature header renamed |
| D11 | medium | Validation loosened below the documented constraint |
| D12 | medium | New 429 response undocumented |
| N01–N04 | decoy | Local rename, added comment, extracted helper, field reorder |

D05, D08 and D10 are the challenging cases the brief asks for. Each is silent:
the code compiles, tests pass, and the endpoint returns 200. The damage lands in
the partner's system: precision loss on money, a 401 where there was none, and
signature verification rejecting every delivery.

Every mutation is verified to compile before it becomes a case. A mutation that
breaks the build tests nothing.

### Metrics

Primary metric is **F1 over verified drift findings** - recall alone rewards
noise, precision alone rewards silence.

| METRIC | SIMPLE BASELINE | AGENT SOLUTION | CHANGE |
|---|---|---|---|
| Recall (drifts caught / 12) | _pending_ | _pending_ | _pending_ |
| Precision | _pending_ | _pending_ | _pending_ |
| F1 (primary) | _pending_ | _pending_ | _pending_ |
| Decoys left clean (/4) | _pending_ | _pending_ | _pending_ |
| Critical drifts caught (/3) | _pending_ | _pending_ | _pending_ |
| Human minutes per repo | _pending_ | _pending_ | _pending_ |
| Cost per repo (USD) | _pending_ | _pending_ | _pending_ |

**Target before running.** A result is only useful to the intended user if it is
worth reading: **recall ≥ 0.80, precision ≥ 0.85, all 3 critical drifts caught,
and all 4 decoys clean.** Below roughly 0.85 precision a reviewer starts
double-checking every finding, at which point the tool has moved the work rather
than removed it. Recorded here before the first run so it cannot be adjusted to
flatter the outcome.

### Baseline

One direct prompt: the handler file, the spec, and "find every place these
disagree." No tools, no verification, no fan-out. Defined in
[baseline/prompt.md](baseline/prompt.md), scored by the same scorer over the
same 16 cases.

---

## Improvement changelog

> Scaffold. Each row gets filled from a real scored run; the evidence column
> links to the run directory under `reports/runs/`.

| STAGE | WHAT WAS TRIED AND WHY | EVIDENCE | DECISION / LEARNING |
|---|---|---|---|
| Baseline | Single prompt with handler + spec, "find disagreements". Establishes what one competent prompt achieves before any agent design. | _pending_ | Starting point |
| Iteration 1 | Add AST route-table extraction as a tool. Hypothesis: the model is worst at exhaustively enumerating routes, which is exactly the part a parser does perfectly. | Tool built and verified: 30/30 checks in `auditor/tools/test_routes.py`. On the real target it finds 841 routes vs 805 grep matches, recovering 50 gin group-root registrations. End-to-end score _pending_. | Kept. Two guards were needed to avoid counting `Header.Get`/`Query().Get` as routes, and the first version of those guards silently dropped every `group.GET("", h)`. Both errors were invisible without a known-answer fixture. |
| Iteration 2 | Add the verification gate - every claim must ship a failing Go test. Hypothesis: most of the baseline's error is false positives, not misses. | _pending_ | _pending_ |
| Iteration 3 | Fan out per endpoint with a reconciler. Hypothesis: whole-repo context dilutes attention on any single contract. | _pending_ | _pending_ |
| Iteration 4 | Add the fintech review skill (money types, idempotency, auth scope, webhook signing). Hypothesis: severity ranking needs domain knowledge the generic model does not apply. | _pending_ | _pending_ |
| Final | Combine what survived | _pending_ | _pending_ |

Removed experiments belong in this table too, with what they taught.

**Main failure mode:** _to be written from evidence._

**Hot take:** _to be written from evidence._

---

## Repository layout

| Path | Contents |
|---|---|
| [auditor/](auditor/) | The agent: tools, prompts, orchestration |
| [baseline/](baseline/) | The single-prompt baseline |
| [eval/fixture/](eval/fixture/) | Synthetic Go payments API + its published spec |
| [eval/mutations/](eval/mutations/) | The 16 mutations and their ground truth |
| [auditor/tools/routes.py](auditor/tools/routes.py) | AST route + annotation extraction (component 1, built) |
| [auditor/tools/goroutes/](auditor/tools/goroutes/) | The `go/ast` walker behind it |
| [eval/inject.py](eval/inject.py) | Builds evaluation cases, verifies each compiles |
| [eval/score.py](eval/score.py) | Scores any run; identical for baseline and agent |
| [eval/oracle.py](eval/oracle.py) | Emits a perfect run, to verify the scorer itself |
| [reports/](reports/) | Run outputs and scored results |
| [REPRODUCTION.md](REPRODUCTION.md) | Clean-environment setup and exact commands |

## Prior work

The target repository and the synthetic fixture's domain shape come from
existing private work that predates this hackathon. Everything in `auditor/`,
`baseline/`, `eval/` and `reports/` was built for it. No credentials from any
private repository appear here; the fixture is synthetic throughout.
