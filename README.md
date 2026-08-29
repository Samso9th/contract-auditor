<p align="center">
  <img src="docs/assets/pr-comment.svg" alt="A pull request comment from contract-auditor listing four verified contract findings, each with the file and line it was found on, and a footer confirming every finding was proved by executing a generated test" width="880">
</p>

# Contract Auditor

**micro1 Agentic Workflows Hackathon**

When a company lets other developers use its software, it publishes a document
describing exactly what each request will do and what will come back. Other teams
read that document and write code against it.

The trouble is that the document and the code are edited by different people at
different times, and nothing checks that they still agree. When they drift apart,
nobody finds out until an outside developer builds something against a promise the
software no longer keeps.

This tool reads both, finds every place they disagree, and then proves each one by
writing a small test and running it. If the test passes, the software was keeping
its promise after all, and the finding is thrown away before anyone sees it.

Nothing reaches the report unproven. That single rule is the whole design.

The findings above are real output from the evaluation in this repository, not a
mockup. Three further claims were refuted by their own tests and never appeared.

---

## Which languages it works with

The tool has to read your code to do its job, so it needs to understand the
language you wrote the software in. Four are supported today.

A language only counts as supported once it has a complete test setup of its own:
a small sample application, a set of deliberately introduced faults, and a scored
run proving the tool finds them. The numbers below are measured, not claimed.
Running `make languages` prints the same table straight from the code.

Everything here comes from the part of the tool that uses no AI at all. It costs
nothing, needs no account, and finishes in under a second:

| Language | Faults it finds without AI | Of what it reported, how much was real | Of what was there, how much it found | Combined score |
|---|---|---|---|---|
| **Go** | 9 kinds | 100% | 80% | **0.889** |
| **TypeScript** (Express) | 8 kinds | 100% | 80% | **0.889** |
| **Python** (FastAPI, Flask) | 8 kinds | 100% | 70% | **0.824** |
| **PHP** (Laravel) | 6 kinds | 100% | 70% | **0.824** |

Two things matter in that table. Everything it reported was real, in every
language, with no false alarms. And it never once raised a complaint about the
sample applications we deliberately left correct.

What it does not find without AI is the same short list everywhere: a field the
code secretly insists on, a rule quietly relaxed, a default value changed. Those
need reading comprehension rather than lookup, and they are what the AI part is
for.

The tool never runs or installs your application. It only reads the source code.
So a project whose dependencies are not installed can still be checked, which
matters because that is the normal state of a fresh checkout.

## Use it in your CI

Packaged as a GitHub Action. The minimum setup needs no API key and no secret,
the deterministic layer costs nothing and catches most drift on its own.

```yaml
name: Contract audit
on: [pull_request]
permissions:
  contents: read
  security-events: write

jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - id: audit
        uses: samso9th/contract-auditor@v1
        continue-on-error: true
        with:
          spec: openapi.json
          source-dir: internal
          strip-prefix: /api/v1
          fail-on: none
      - uses: github/codeql-action/upload-sarif@v3
        if: always()
        with:
          sarif_file: ${{ steps.audit.outputs.sarif }}
```

Findings then appear as annotations on the exact lines of the pull request diff.
Add `api-key` to enable the judgment pass, and `webhook-url` to POST the full
report to Slack, Telegram or a Postgres sink.

**Full setup, every input, and troubleshooting: [docs/site/github-actions.mdx](docs/site/github-actions.mdx)**, published at the docs site.


## Who this is for, and what goes wrong

Any company that publishes software for other developers to use, and every
developer building against it.

The case that prompted this is a payments company. Other financial companies read
its published document, write code to move money through it, and go live. When
the document and the running software disagree, the failure lands in someone
else's business, often involving real money.

A published service ends up with three separate descriptions of itself, and
nothing keeps them in step:

1. the code, which is what actually runs;
2. short notes written above the code, which a tool turns into the formal document;
3. the human-readable guide that outside developers actually read.

Different people edit each one, at different times. Code review asks whether the
code is correct. It does not ask whether the code still matches what was promised
months ago, so the two drift apart quietly.

The measurements below come from that payments company's own codebase, which has
roughly 324 files of code:

| What we counted | Number | Why it matters |
|---|---|---|
| Requests the software actually answers | 841 | What really exists |
| Requests described in the published document | 170 | What outside developers can see |
| Requests with no description written for them | 500 of 841 (59%) | Invisible to anyone outside the company |
| Descriptions that say only "some object comes back" | 153 of 194 (79%) | Technically present, practically useless |

That first number was counted by the tool itself, not estimated. A crude text
search finds 805, which is both too many and too few: it counts things that only
look like request definitions, and misses 50 real ones written in a shorthand it
cannot recognise. The gap between 805 and 841 is the whole argument in miniature.
An approximate answer and a correct one are not the same answer.

Not all 841 of those are meant to be public. Internal and staff-only functions
are legitimately left undocumented, and working out which is part of the job. But
nobody is comparing 170 descriptions against 841 real ones by hand, which is
exactly why nobody had ever measured the gap.

The cost of this problem is lopsided. It is nearly free to fix on the day someone
introduces it, and expensive once an outside company has built against the wrong
promise. Checking automatically on every change moves the cost back to where it
belongs.

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
  that genuinely needs judgment: reconciling a handler body against prose.
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
this repo at [eval/fixture](eval/fixture): ten endpoints, standard library
only, builds offline, with a committed OpenAPI spec that matches it exactly in
the clean state.

Drift is then **injected**, so the ground truth is known exactly rather than
argued about. This is what makes the numbers below checkable by anyone.

The private repository is reported separately as a case study, with findings
quoted as evidence and no credentials included.

### The cases

16 cases, exceeding the brief's target of ten: **12 injected drifts** across the
categories that actually hurt integrators, and 4 decoys: refactors that change
code without changing the contract. The decoys are what make precision
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

Primary metric is F1 over verified drift findings. Recall alone rewards noise,
precision alone rewards silence.

| METRIC | SIMPLE BASELINE | AGENT SOLUTION | CHANGE |
|---|---|---|---|
| **F1 (primary)** | 0.231 | **1.000** | **+0.769** |
| Recall (drifts caught / 15) | 0.400 (6) | **1.000** (15) | +0.600 |
| Precision | 0.162 | **1.000** | +0.838 |
| False positives | 31 | **0** | −31 |
| Critical drifts caught (/5) | 2 | **5** | +3 |
| Decoys left clean (/4) | 4 | 4 | n/a |
| Cost per repo (USD) | $0.0160 | $0.0441 | +$0.0281 |
| Human minutes per repo | 0 | 0 | n/a |

The baseline is not merely worse, it is differently wrong. It reported **31 false
positives against 6 true findings**: five spurious findings on D02 alone, twelve
on D05, so a reviewer would have to check 37 findings to reach 6 real ones. It
also missed every drift requiring cross-file reasoning: the renamed field (D01),
the unregistered route (D04), the auth change (D08). Its 4/4 on decoys is not
discrimination; it invented findings on the drift cases instead.

Cost is worth reading carefully. The agent costs 2.8x the baseline per repo and
still lands at four cents. Latency is the real price: the agent made 159 model
calls against the baseline's 16.

A result is only useful to the
intended user if it is worth reading: **recall ≥ 0.80, precision ≥ 0.85, every
critical drift caught, and all 4 decoys clean.** Below roughly 0.85 precision a
reviewer starts double-checking every finding, at which point the tool has moved
the work rather than removed it.

All four met: recall 1.00, precision 1.00, 5/5 critical, 4/4 decoys. The bar was
written down before any run so it could not be adjusted to flatter the outcome.

The gain comes from one place. The deterministic layer alone scores F1 0.889 at
precision 1.0 for nothing: no model, no key, 0.2 seconds. The agent's whole
contribution is the last 0.20 of recall: D06, D09 and D11, the three drifts that
need judgment rather than lookup.

### Baseline

One direct prompt: the handler file, the spec, and "find every place these
disagree." No tools, no verification, no fan-out. Defined in
[baseline/prompt.md](baseline/prompt.md), scored by the same scorer over the
same 16 cases.

---

## Improvement changelog

Every row is a real scored run over the same 16 cases with the same scorer.
Evidence links to `reports/runs/`. Experiments that were removed are here too,
with what they taught.

| STAGE | WHAT WAS TRIED AND WHY | EVIDENCE | DECISION / LEARNING |
|---|---|---|---|
| Baseline | Single prompt per case: the whole Go package, the whole spec, "find every place these disagree". No tools, no verification. Establishes what one competent prompt achieves before any agent design. | **6 true findings against 31 false positives. Precision 0.162, recall 0.400, F1 0.231**, $0.016 (`reports/runs/baseline/`). | Starting point, and a sobering one. It is not merely worse; it is differently wrong. Twelve spurious findings on D05 alone, and it missed every drift needing cross-file reasoning (D01 renamed field, D04 unregistered route, D08 auth change). A reviewer would check 37 findings to reach 6 real ones. |
| Iteration 1 | Add AST route-table extraction as a tool. Hypothesis: the model is worst at exhaustively enumerating routes, which is exactly the part a parser does perfectly. | Tool built and verified: 30/30 checks in `auditor/tools/test_routes.py`. On the real target it finds 841 routes vs 805 grep matches, recovering 50 gin group-root registrations. Route extraction is exact on the fixture and stable across runs. | Kept. Two guards were needed to avoid counting `Header.Get`/`Query().Get` as routes, and the first version of those guards silently dropped every `group.GET("", h)`. Both errors were invisible without a known-answer fixture. |
| Iteration 1b | Extend the parser to struct shapes and handler-body facts (status codes, query keys, headers read and set), then write nine deterministic rules over them. Hypothesis: a large share of contract drift is mechanical and needs no judgment at all. | 12/15 drifts caught, **0 false positives, 4/4 decoys clean, F1 0.889, $0.00, 0.2s** across all 16 cases (`reports/runs/deterministic/`; 27/27 checks in `test_diff.py`). | Kept, and it reframes the project. The no-model layer already meets every target committed before the first run. The three it misses (D06, D09, D11) are exactly the three that need judgment rather than lookup, so that is now the agent's job, under a precision floor of 1.0 it must not lower. |
| Iteration 2 | Add the verification gate: every claim must ship a Go test that executes the real handler through `httptest` and asserts what the spec promises. A claim is confirmed only if that test **fails**. Hypothesis: most of the error in any claim-generating layer is false positives, not misses. | Gate built and verified: 29/29 checks in `auditor/test_verify.py`. Confirms all 7 executable true claims with evidence taken from the running handler (`spec declares "available" as string; response carries number (2.45e+06)`), refutes all 6 of the same claims against clean code, rejects fabricated field names, and refutes claims against all 3 decoys. | Kept. Building it surfaced its own worst failure: the first version generated a test asserting that the claimed field was present, so a hallucinated field name produced a failing test and a *confirmed* finding; the gate would have laundered hallucinations rather than caught them. A claim must now name a field the spec actually documents before any test is generated. |
| Iteration 2b | Route model calls through OpenRouter behind a provider-neutral client rather than binding to one vendor SDK. Hypothesis: vendor choice is a cost lever, not an architectural one, and judges should be able to run this with whatever key they hold. | Client built and verified: 20/20 in `auditor/test_llm.py`, live half costing $0.000508. Benchmarked on one identical prompt: `z-ai/glm-5.3-flash` $0.000046, `moonshotai/kimi-k2.7-code` $0.000118, `moonshotai/kimi-k3` $0.002428, a 53x spread for the same answer. | Kept, default `glm-5.3-flash`. Three things only measurement would have shown: GLM 5.3 Flash is a reasoning model whose reasoning cannot be disabled (`reasoning:{enabled:false}` → 400) and which returns empty content if `max_tokens` only fits the answer; `kimi-k2.7-code` prefixes its JSON with a space; and Kimi K3, despite the name suggesting an upgrade, costs more per token than Claude Sonnet 5 and was dropped. |
| Iteration 3 | Fan out per endpoint with the agent restricted to the three kinds the rules cannot settle, and told what they already found. Hypothesis: whole-repo context dilutes attention, and an unconstrained vocabulary invites restating mechanical findings. | Agent recovered all three judgment drifts (D06, D09, D11) that the deterministic layer misses. Combined: **precision 1.0, recall 1.0, F1 1.0, 4/4 decoys clean, $0.0441, 26 min, 159 model calls** (`reports/runs/agent/`). | Kept. One endpoint per call, plus handing the agent the deterministic findings, produced no duplicate reports across 16 cases. |
| Iteration 4 | Extend the verification gate to execute the three judgment kinds, rather than letting them through unverified because no parser could check them. | In the shipped run (`reports/runs/agent/`) the agent proposed **6 claims of which 3 were false**; the gate refuted all 3, confirmed the 3 true ones, and refuted **0 of the 12 deterministic claims**. An earlier identical run proposed **13 claims of which 10 were false**. Both scored F1 1.000. | Kept, and it is the load-bearing result, for a reason the two runs make clearer than either does alone. The count of *true* claims was stable at 3; the count of *false* ones moved from 3 to 10 with no change to prompt, model or cases. Ungated, that variance would have put final precision somewhere between 0.60 and 0.83 depending on the day. Gated, both runs produced the identical report. The gate does not only raise precision, it removes the variance. |
| Iteration 5 *(removed)* | Add a fintech review skill (money types, idempotency, auth scope, webhook signing) to improve severity ranking. | Not built. Severity was already assigned per rule, with money-field name hints escalating a type mismatch to critical in `diff.py`. D05 and D08 both surfaced as critical without it, and the run had zero severity complaints to fix. | **Removed before building.** The hypothesis assumed a problem the measurement did not show. A skill file nothing loads is a component added for the look of the architecture, and the brief is explicit that purposeful choices matter more than the number of components. Recorded here because deciding *not* to build something on evidence is the same skill as building it. |
| Final | Deterministic rules → constrained per-endpoint agent → verification gate over every claim, whatever produced it. | **Precision 1.000, recall 1.000, F1 1.000, 15/15 drifts, 0 false positives, 4/4 decoys clean, 5/5 critical drifts, 159 model calls, $0.0704, 17 min** (`reports/runs/agent/`). Against the baseline: **F1 0.231 → 1.000**. Reproduced: two independent full runs, days apart in development and differing in agent noise, both scored F1 1.000. | The gate is the main contribution. Everything else is ordinary engineering; the gate is what makes it safe to let a model guess at all. |

Removed experiments belong in this table too, with what they taught.

**Main failure mode: an agent's confident wrong answer is indistinguishable from its right one, and so is a broken run.**

Measured, not asserted. Across 16 cases the per-endpoint agent produced 6 claims
and **3 of them were false**. Every one was fluent, specific, and cited a
plausible line. Nothing in the text separated them from the 3 that were true.

An earlier run of the same code over the same cases produced **13 claims, 10 of
them false**. The number of *true* claims did not move; it was 3 both times,
because there are 3 judgment drifts to find. What moved was the noise, by more
than a factor of three, with no change to the prompt, the model, or the inputs.
You cannot tune against that. There is no prompt that fixes a number which
varies this much between identical runs.

The same shape appeared three more times, and each was invisible until something
executed it:

- An **unparseable model reply produced zero claims**, which is byte-identical to
  "this endpoint is clean". A transport failure looked like a passing audit.
- A **skipped Go test exits 0**, and an exit-code check read that as "claim
  refuted". A true finding was silently deleted.
- The **route extractor returned different answers on five identical runs**.
  Go randomises map iteration, so the report changed while the code did not.

None of these announce themselves. All of them fail toward *looking fine*.

**Hot take: stop trying to make the agent right, and build the thing that can
prove it wrong.**

The instinct on seeing precision like that is to fix the model: better prompt, better
model, more context. We did tune the prompt, and it mattered in one specific way
worth naming: when the spec index dropped `minLength`/`maxLength`, the model was
shown `bvn: {"type": "string"}` for a field constrained to exactly 11 characters
and produced a confident finding against code that was honouring the spec
exactly. **Starving a reader of context does not make it cautious; it makes it
reason in circles and then be confidently wrong.** Restoring the constraints cut
its output on those prompts from >8000 tokens (truncating, timing out) to under
1900, and the false positive disappeared.

But prompt work has a ceiling, and it is not a guarantee. What changed the
outcome was charging every claim the price of a test that executes the real
handler. The agent's raw precision was 0.50 on the shipped run and 0.23 on the
earlier one; both became 1.00, with recall going up rather than down. Two
runs whose raw noise differed threefold produced byte-identical reports. The
agent became *more* useful once it was allowed to be wrong, because being wrong
stopped being expensive, and it stopped mattering how wrong it was on any given
day.

That inverts the usual build order. The deterministic layer alone scores F1 0.889
for zero cost: most of this problem never needed an agent. The agent earns its
place on exactly three of fifteen drifts, and only because a gate stands between
its guesses and the report. **Build the refutation mechanism first; it tells you
how much agent you actually need, and it is the only thing that makes the answer
trustworthy when you do.**

---

## Repository layout

| Path | Contents |
|---|---|
| [auditor/](auditor/) | The agent: tools, prompts, orchestration |
| [auditor/notify.py](auditor/notify.py) | Slack/Telegram alerting, gated on verification |
| [ddocs/](ddocs/) | Plain-language overview, self-improvement note, GHCR publishing guide |
| [docs/site/](docs/site/) | Public documentation site: quickstart, CI setup, how it works |
| [baseline/](baseline/) | The single-prompt baseline |
| [eval/fixture/](eval/fixture/) | Synthetic Go payments API + its published spec |
| [eval/mutations/](eval/mutations/) | The 16 mutations and their ground truth |
| [auditor/tools/routes.py](auditor/tools/routes.py) | AST route + annotation extraction (built) |
| [auditor/tools/spec.py](auditor/tools/spec.py) | OpenAPI index with `$ref` resolution (built) |
| [auditor/tools/diff.py](auditor/tools/diff.py) | Nine deterministic drift rules (built) |
| [auditor/tools/goroutes/](auditor/tools/goroutes/) | The `go/ast` walker behind them |
| [auditor/run_deterministic.py](auditor/run_deterministic.py) | Scores the no-model layer over all 16 cases |
| [auditor/verify.py](auditor/verify.py) | The verification gate: generates and runs a Go test per claim (built) |
| [eval/inject.py](eval/inject.py) | Builds evaluation cases, verifies each compiles |
| [eval/score.py](eval/score.py) | Scores any run; identical for baseline and agent |
| [eval/oracle.py](eval/oracle.py) | Emits a perfect run, to verify the scorer itself |
| [reports/](reports/) | Run outputs and scored results |
| [action.yml](action.yml) · [Dockerfile](Dockerfile) | The GitHub Action and its image |
| [REPRODUCTION.md](REPRODUCTION.md) | Clean-environment setup and exact commands |

## Prior work

The target repository and the synthetic fixture's domain shape come from
existing private work that predates this hackathon. Everything in `auditor/`,
`baseline/`, `eval/` and `reports/` was built for it. No credentials from any
private repository appear here; the fixture is synthetic throughout.
