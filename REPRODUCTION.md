# Reproduction guide

Written for someone starting from a clean machine with no prior context. Every
command is copy-pasteable from the repository root.

## Requirements

| Tool | Version used | Why |
|---|---|---|
| Go | 1.22+ (developed on 1.26.3) | Builds the fixture and runs verification tests |
| Python | 3.9+ (standard library only) | Case injection and scoring - no pip install needed |
| Anthropic API key | - | Required only for the agent and baseline runs, not for building or scoring cases |

No database, no Docker, no network access is needed to build the evaluation
cases. The fixture uses only the Go standard library.

Verify your toolchain:

```bash
go version
python3 --version
```

## 1. Build the evaluation cases

```bash
cd eval
python3 inject.py --list     # see all 16 cases
python3 inject.py --all      # build them
```

Expected output: 16 lines each ending `build ok`, then
`16 cases: 12 with drift, 4 decoys`.

Each case lands in `eval/cases/<ID>/` containing `api/` (mutated Go source),
`spec/openapi.json` (the unchanged published spec), and `ground_truth.json`.
`inject.py` compiles every mutated fixture and aborts if one does not build, so
a green run means all 16 cases are valid targets.

Runtime: under 30 seconds. Cost: none.

## 1b. Verify the deterministic tools

The auditor's non-model components are checked against answers known by
construction, with no API key and no cost:

```bash
make test-tools
```

Expected: `30/30 checks passed`. This builds the Go route extractor on first run
(a few seconds) and asserts the route table is exactly right on the clean
fixture and on every mutated case.

To see the extracted table for yourself:

```bash
make routes
```

## 2. Run the baseline

```bash
export ANTHROPIC_API_KEY=sk-...
python3 baseline/run.py --cases eval/cases --out reports/runs/baseline
```

Writes one findings file per case to `reports/runs/baseline/`.

Runtime: ~3 minutes. Approximate cost: _pending first full run._

## 3. Run the agent

```bash
python3 auditor/run.py --cases eval/cases --out reports/runs/agent
```

Same inputs, same output format, same 16 cases. The only difference is what
happens between reading the case and writing the findings.

Runtime: ~12 minutes. Approximate cost: _pending first full run._

## 4. Score both

```bash
cd eval
python3 score.py --run ../reports/runs/baseline --markdown
python3 score.py --run ../reports/runs/agent --markdown
```

Prints a per-case table and a summary containing the primary metric (F1),
precision, recall, decoys left clean, total cost and wall-clock time. Add
`--json` for machine-readable output.

## What you should see

The agent should catch more drift than the baseline *and* stay clean on more
decoys. The specific claim under test is that the verification gate raises
precision without costing recall - so compare the `precision` and `recall` rows
between the two runs, not just F1.

Targets committed before the first run: recall ≥ 0.80, precision ≥ 0.85, all 3
critical drifts (D05, D08, D10) caught, all 4 decoys clean.

## Verifying the scorer

The scorer can be checked without spending a token by feeding it the ground
truth as if it were a perfect run:

```bash
cd eval
python3 oracle.py
python3 score.py --run ../reports/runs/oracle
```

Or in one step from the repository root, which also confirms the fixture builds
and all 16 cases compile:

```bash
make check
```

Expected: 12 true positives, 0 false positives, 0 false negatives, precision
1.0, recall 1.0, decoys clean 4/4. Any other result means the scorer is broken,
not the agent.

## Resetting

```bash
rm -rf eval/cases reports/runs
```

`eval/fixture/` is the pristine source and is never mutated in place - every
mutation is applied to a copy under `eval/cases/`.

## Auditing a real repository

```bash
python3 auditor/run.py --repo /path/to/go/api --spec /path/to/openapi.json \
  --docs /path/to/docs --out reports/runs/<name>
```

There is no ground truth for a real repository, so this produces a report rather
than a score. Findings are ranked by severity, and each carries the test that
proves it.
