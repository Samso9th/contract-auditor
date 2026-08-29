# Does it actually work?

The evaluation behind the headline numbers in the [README](README.md): what the
tool was tested against, every test case, the scored results against a
single-prompt baseline, and the bar we set before running anything.

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
