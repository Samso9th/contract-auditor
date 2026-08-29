# Contract Auditor — plain-language overview

Written for a non-technical reader: what this project is, the problem it solves,
how it solves it, and who it is for. The technical detail lives in
[../README.md](../README.md); this page deliberately avoids it.

---

## What it is

A robot reviewer that checks whether a company's software **actually does what
its instruction manual says it does** — and that doesn't merely claim a
mismatch, it proves each one.

---

## The problem

When a company builds an API (a way for other companies' software to plug into
theirs), that API ends up described in **three separate places**:

1. **The code** — what really happens when someone calls it.
2. **The generated technical spec** — a machine-readable summary built from
   short notes developers write above their code.
3. **The written documentation** — the human-readable guide outside partners
   actually read.

Different people edit these at different times, and nothing forces them to stay
in sync. A developer changes the code; nobody updates the docs. The manual now
lies.

Ordinary code review asks *"is this code correct?"* — not *"does this code still
match what we promised?"* So the lies accumulate quietly.

### The motivating case

Hyparrow, a Nigerian payments API. Measured facts from that codebase:

| What was measured | Value |
|---|---|
| Endpoints that exist in the code | 841 |
| Endpoints described in the published spec | 170 |
| Endpoints carrying no documentation note at all | 500 of 841 (59%) |
| Documented responses that say only "returns an object" | 153 of 194 (79%) |

**An important caveat on those numbers.** The gap between 841 and 170 is *not*
671 missing documents. Most endpoints in any real API are never meant to be
public — admin screens, internal service-to-service calls, health checks,
operational tooling. Those legitimately stay out of developer documentation, and
publishing them would be a mistake, not a fix.

So the honest reading is: **841 endpoints exist, 170 are published, and nobody
knows which of the remaining 671 *should* have been.** Sorting the deliberately
internal from the accidentally missing is part of the job this tool does, not a
step it skips. The number to worry about is not the size of the gap — it is that
the gap has never been classified.

The last row is the one that stings regardless of classification: for 79% of
what *is* published, the documentation says an object comes back and nothing
about what is inside it. That is a documented endpoint that still can't be built
against.

### Why it costs money

A mismatch costs almost nothing to fix on the day it is created. It costs a lot
once a partner company has built their product against the wrong description —
because the failure then surfaces in *their* system, in production, with no
error message pointing at the real cause.

Catching it at the commit moves the cost back to whoever created it.

---

## The solution

Three ideas, in order of importance.

### 1. Let a computer do the counting; let AI do the judging

Listing every endpoint is boring, exact work — a parser does it perfectly, AI
does it approximately. So a conventional program builds the exhaustive list and
computes the differences. The AI is spent only on the part that needs real
judgment: *does this code's behaviour match this English paragraph?*

A small illustration of why this split matters: a naive text search of the
target codebase finds 805 endpoints. The proper parser finds 841 — the search
counted things that weren't endpoints, and missed 50 that were. Close enough and
correct are not the same answer.

### 2. The verification gate — the actual contribution

AI's characteristic failure is a **confident, plausible, wrong** finding. So
nothing enters the report unless the AI can write a small automated test that
*fails* because of the problem. If the test passes, the AI was wrong, and the
finding is discarded silently.

That converts the report from **opinion** into **evidence**. Every problem in
the output arrives with a receipt.

### 3. Memory

Some divergence is intentional — an internal endpoint that is *supposed* to stay
undocumented. Mark it once, and it stops being re-reported. Repeat runs stay
signal instead of decaying into noise.

---

## Use case illustration

An engineer at a payments company makes what looks like a harmless cleanup: the
transaction amount changes from text (`"1500.50"`) to a decimal number
(`1500.50`).

Everything looks fine. The code compiles. Tests pass. The endpoint returns
success.

Months later, a partner fintech's system starts producing amounts off by a
fraction of a kobo on certain transactions — because decimal numbers in
computers cannot represent money exactly. Thousands of transactions later the
ledgers don't balance, and nobody knows why.

With this tool, on the day of the change, CI would have flagged:

> **CRITICAL** — `/payouts`: `amount` is a decimal number in code but documented
> as a text string. Partners parsing it as text will break; parsing it as a
> number loses precision.
> *Proof: attached test, which fails against the current code.*

Two more cases of the same shape are in the test set:

- an endpoint that quietly started requiring a login while still documented as
  public — partners are suddenly locked out;
- a webhook security header that was renamed — every incoming notification now
  fails its security check and is rejected.

All three are **silent**. Nothing crashes. The damage lands in someone else's
system.

---

## Who can use this

| User | Why |
|---|---|
| **Backend teams publishing an API to outside partners** | The primary user. Run it in CI so drift is caught at the commit that caused it. |
| **Fintechs and payments companies** | The tool carries domain knowledge about what actually hurts here: money precision, authentication scope, webhook signatures, duplicate-payment protection. |
| **Companies integrating with someone else's API** | Point it at the documentation you were handed and find out what is wrong with it before you build on it. |
| **Platform / developer-experience teams** | Owners of documentation quality who need a number rather than a hunch for how bad the drift is. |
| **Due-diligence and audit** | Vendor assessment, security review, acquisition technical diligence. |

Built for **Go** APIs at present. The approach is not Go-specific.

---

## Current state, honestly

What is **built and working** is the deterministic half: the endpoint extractor
(passing 30/30 correctness checks), the 16-case test suite with known-correct
answers, the scorer, and the scorer's own self-test.

What is **not yet written** is the AI agent itself (`auditor/run.py`) and the
simple comparison it is meant to beat (`baseline/run.py`). Consistent with that,
every results figure in the README currently reads `_pending_`.

That is a defensible order to work in — the success targets (recall ≥ 0.80,
precision ≥ 0.85) are committed in writing *before* any run, so they cannot be
quietly lowered afterwards to flatter the result. But the scoreboard is still
empty, and any presentation of this work should say so.
