# Does it actually work?

The evaluation behind the headline numbers in the [README](README.md), and the
build history behind the design: what the tool was tested against, every test
case, the scored results against a single-prompt baseline, the bar we set before
running anything, and every idea we tried, kept or abandoned along the way.

## What it was tested against

The payments company's real codebase is private and its working files contain
live passwords, so it cannot be handed to judges. Instead this repository
contains a small working payments service written for the purpose: eight
kinds of request, nothing to install, runs without internet, and a published
document that matches it exactly.

Faults are then deliberately introduced, one per test case. Because we broke it
on purpose, we know precisely what a correct answer looks like, which is what
makes every number below checkable by anyone who clones this.


## The test cases

16 cases. Twelve contain a real fault of the kind that hurts outside developers.
The other four contain a change that looks like a fault but is not: a variable
renamed, a comment added, a function split in two, fields reordered. The
behaviour is identical.

Those four are the important ones. Without them, a tool that complains about
everything would score perfectly.

| ID | Severity | What was broken |
|---|---|---|
| D01 | high | A field was renamed in the code, the document still promises the old name |
| D02 | medium | The code reports a different success signal than documented |
| D03 | high | A request exists in the code that the document never mentions |
| D04 | high | The document promises a request the code no longer answers |
| D05 | **critical** | A money amount changed from exact text to a rounded number |
| D06 | high | The code demands a field the document says is optional |
| D07 | high | A setting is read under a different name than documented |
| D08 | **critical** | A request now requires a password; the document says it is open to all |
| D09 | low | A default value changed and the document was not updated |
| D10 | **critical** | The security stamp on outgoing messages was renamed |
| D11 | medium | A rule was relaxed below what the document promises |
| D12 | medium | A new failure response was added and never documented |
| N01–N04 | not a fault | Rename, comment, refactor, reorder. Behaviour unchanged |

D05, D08 and D10 are the hardest three, and all three are silent. The code builds,
the existing tests pass, and the request still succeeds. The damage lands in
someone else's system: money quietly losing accuracy, a login demanded where none
was before, and every outgoing security stamp being rejected because it arrives
under a different name.

Every introduced fault is checked to make sure the software still builds before it
counts as a test case. A fault that breaks the build tests nothing.

## The results

Compared against the obvious simple approach: hand the whole codebase and the
whole document to an AI in one go and ask it to find the disagreements. Same AI,
same test cases, same scoring.

| | Simple approach | This tool | Change |
|---|---|---|---|
| **Overall score** | 0.219 | **1.000** | **+0.781** |
| Real faults found, of 15 | 15 | **15** | same |
| Of what it reported, how much was real | 12% | **100%** | +88 points |
| False alarms | 107 | **0** | −107 |
| Correct code wrongly flagged, of 4 | 4 | **0** | −4 |
| Serious faults found, of 5 | 5 | **5** | same |
| Cost per run | $0.166 | **$0.070** | −$0.096 |
| Human time | none | none | same |

The simple approach is not blind. It found every one of the 15 real faults. It
also reported 107 things that were not there, and complained about all four
pieces of correct code. Reading its output means checking 122 complaints to find
15 real ones, and being told four healthy things are broken.

That is worse than useless, because a reviewer who checks ten false alarms in a
row stops reading the eleventh. Finding everything is not the hard part. Not
crying wolf is the hard part, and that is what the proving step buys.

It also costs less than half as much, because asking about one request at a time
is cheaper than repeatedly handing over an entire codebase.

**What we said "good" meant, before running anything.** Written down in advance so
it could not be adjusted afterwards to flatter the result: at least 80% of real
faults found, at least 85% of reports being real, every serious fault caught, and
no complaints about the four healthy pieces of code. All four were met.

The AI is not doing most of the work here, and that is worth being honest about.
The no-AI stage alone scores 0.889 for free. The AI earns its place on three
faults out of fifteen, the three that need judgement rather than lookup, and only
because every one of its suggestions has to survive a real test before anyone
sees it.

---

## What we tried, in order

This is the build history: every idea, what it scored, and what we decided. It is
the most technical part of this document, and it is here because the honest
version of "did this work" is a list of things that did and did not.

Every row is a real scored run over the same 16 test cases with the same scoring.
The evidence links to saved output in `reports/runs/`. Ideas we abandoned are
here too, with what they taught us.

| STAGE | WHAT WAS TRIED AND WHY | EVIDENCE | DECISION / LEARNING |
|---|---|---|---|
| Baseline | Hand the whole codebase and the whole document to the AI in one go and ask it to find every disagreement. No tools, no proving step. Establishes what one good prompt achieves before building anything. | **All 15 real faults found, alongside 107 false alarms. 12% of what it reported was real. All 4 healthy files wrongly flagged. F1 0.219**, $0.166 (`reports/runs/baseline/`). | Sobering, and not in the way we expected. It is not blind: it finds everything. It is indiscriminate. A reviewer would check 122 complaints to reach 15 real ones, and be told four healthy things were broken. Finding faults is not the hard part. Not crying wolf is. |
| Iteration 1 | Add AST route-table extraction as a tool. Hypothesis: the model is worst at exhaustively enumerating routes, which is exactly the part a parser does perfectly. | Tool built and verified: 30/30 checks in `auditor/tools/test_routes.py`. On the real target it finds 841 routes vs 805 grep matches, recovering 50 gin group-root registrations. Route extraction is exact on the fixture and stable across runs. | Kept. Two guards were needed to avoid counting `Header.Get`/`Query().Get` as routes, and the first version of those guards silently dropped every `group.GET("", h)`. Both errors were invisible without a known-answer fixture. |
| Iteration 1b | Extend the parser to struct shapes and handler-body facts (status codes, query keys, headers read and set), then write nine deterministic rules over them. Hypothesis: a large share of contract drift is mechanical and needs no judgment at all. | 12/15 drifts caught, **0 false positives, 4/4 decoys clean, F1 0.889, $0.00, 0.2s** across all 16 cases (`reports/runs/deterministic/`; 27/27 checks in `test_diff.py`). | Kept, and it reframes the project. The no-model layer already meets every target committed before the first run. The three it misses (D06, D09, D11) are exactly the three that need judgment rather than lookup, so that is now the agent's job, under a precision floor of 1.0 it must not lower. |
| Iteration 2 | Add the verification gate: every claim must ship a Go test that executes the real handler through `httptest` and asserts what the spec promises. A claim is confirmed only if that test **fails**. Hypothesis: most of the error in any claim-generating layer is false positives, not misses. | Gate built and verified: 29/29 checks in `auditor/test_verify.py`. Confirms all 7 executable true claims with evidence taken from the running handler (`spec declares "available" as string; response carries number (2.45e+06)`), refutes all 6 of the same claims against clean code, rejects fabricated field names, and refutes claims against all 3 decoys. | Kept. Building it surfaced its own worst failure: the first version generated a test asserting that the claimed field was present, so a hallucinated field name produced a failing test and a *confirmed* finding; the gate would have laundered hallucinations rather than caught them. A claim must now name a field the spec actually documents before any test is generated. |
| Iteration 2b | Route model calls through OpenRouter behind a provider-neutral client rather than binding to one vendor SDK. Hypothesis: vendor choice is a cost lever, not an architectural one, and judges should be able to run this with whatever key they hold. | Client built and verified: 20/20 in `auditor/test_llm.py`, live half costing $0.000508. Benchmarked on one identical prompt: `z-ai/glm-5.3-flash` $0.000046, `moonshotai/kimi-k2.7-code` $0.000118, `moonshotai/kimi-k3` $0.002428, a 53x spread for the same answer. | Kept, default `glm-5.3-flash`. Three things only measurement would have shown: GLM 5.3 Flash is a reasoning model whose reasoning cannot be disabled (`reasoning:{enabled:false}` → 400) and which returns empty content if `max_tokens` only fits the answer; `kimi-k2.7-code` prefixes its JSON with a space; and Kimi K3, despite the name suggesting an upgrade, costs more per token than Claude Sonnet 5 and was dropped. |
| Iteration 3 | Fan out per endpoint with the agent restricted to the three kinds the rules cannot settle, and told what they already found. Hypothesis: whole-repo context dilutes attention, and an unconstrained vocabulary invites restating mechanical findings. | Agent recovered all three judgment drifts (D06, D09, D11) that the deterministic layer misses. Combined: **precision 1.0, recall 1.0, F1 1.0, 4/4 decoys clean, $0.0441, 26 min, 159 model calls** (`reports/runs/agent/`). | Kept. One endpoint per call, plus handing the agent the deterministic findings, produced no duplicate reports across 16 cases. |
| Iteration 4 | Extend the verification gate to execute the three judgment kinds, rather than letting them through unverified because no parser could check them. | In the shipped run (`reports/runs/agent/`) the agent proposed **6 claims of which 3 were false**; the gate refuted all 3, confirmed the 3 true ones, and refuted **0 of the 12 deterministic claims**. An earlier identical run proposed **13 claims of which 10 were false**. Both scored F1 1.000. | Kept, and it is the load-bearing result, for a reason the two runs make clearer than either does alone. The count of *true* claims was stable at 3; the count of *false* ones moved from 3 to 10 with no change to prompt, model or cases. Ungated, that variance would have put final precision somewhere between 0.60 and 0.83 depending on the day. Gated, both runs produced the identical report. The gate does not only raise precision, it removes the variance. |
| Iteration 5 *(removed)* | Add a fintech review skill (money types, idempotency, auth scope, webhook signing) to improve severity ranking. | Not built. Severity was already assigned per rule, with money-field name hints escalating a type mismatch to critical in `diff.py`. D05 and D08 both surfaced as critical without it, and the run had zero severity complaints to fix. | **Removed before building.** The hypothesis assumed a problem the measurement did not show. A skill file nothing loads is a component added for the look of the architecture, and the brief is explicit that purposeful choices matter more than the number of components. Recorded here because deciding *not* to build something on evidence is the same skill as building it. |
| Final | Deterministic rules → constrained per-endpoint agent → verification gate over every claim, whatever produced it. | **Precision 1.000, recall 1.000, F1 1.000, 15/15 drifts, 0 false positives, 4/4 decoys clean, 5/5 critical drifts, 159 model calls, $0.0704, 17 min** (`reports/runs/agent/`). Against the baseline: **F1 0.219 → 1.000**, with false alarms going from 107 to 0. Reproduced: two independent full runs, days apart in development and differing in agent noise, both scored F1 1.000. | The gate is the main contribution. Everything else is ordinary engineering; the gate is what makes it safe to let a model guess at all. |

Removed experiments belong in this table too, with what they taught.

## The main thing that goes wrong

**When an AI is wrong, it looks exactly the same as when it is right. So does a
broken run.**

Measured, not guessed. Across the 16 test cases the AI made 6 suggestions and 3
of them were false. Every one was fluent, specific, and pointed at a real line of
code. Nothing in the writing separated the wrong ones from the right ones.

An earlier run of identical code over identical cases made 13 suggestions, 10 of
them false. The number of *correct* suggestions did not move: 3 both times,
because there are 3 such faults to find. What moved was the noise, by more than
three times, with nothing changed at all. You cannot write a better prompt to fix
a number that swings that much between identical runs.

The same shape showed up three more times while building this, and each was
invisible until something actually ran:

- A reply the tool could not read produced zero complaints, which looks
  identical to "this part is fine". A network failure looked like a clean report.
- A test that was skipped rather than run exits successfully, and a check on that
  exit code read it as "the complaint was wrong". A true finding was deleted.
- The code-reading step gave different answers on five identical runs, because of
  an ordering quirk in the language it was written in. The report changed while
  the code did not.

None of these announce themselves. All of them fail in the direction of looking
fine.

## What we would tell someone building something similar

**Stop trying to make the AI right. Build the thing that can prove it wrong.**

The instinct on seeing half the AI's answers be wrong is to fix the AI: a better
prompt, a better model, more context. We did improve the prompt, and it mattered
in one specific way. When we accidentally hid part of the documentation from it,
it produced a confident complaint about code that was behaving perfectly.
Starving it of context did not make it cautious. It made it reason in circles and
then be confidently wrong.

But prompt work has a ceiling and it is never a guarantee. What actually changed
the outcome was making every single complaint pay for itself with a test that
runs against the real code. The AI's raw accuracy was 50% on one run and 23% on
another. Both became 100% after the proving step, and it found *more*, not less.
Two runs whose raw noise differed threefold produced identical reports.

The AI became more useful once it was allowed to be wrong, because being wrong
stopped mattering.

That reverses the order most people build in. The no-AI part alone scores 0.889
for nothing. The AI earns its keep on 3 faults out of 15, and only because
something stands between its guesses and the report. Build the thing that can
prove it wrong first. It tells you how much AI you actually need, and it is the
only reason to trust the answer when you do.
