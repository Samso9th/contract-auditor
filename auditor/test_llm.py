#!/usr/bin/env python3
"""Verification for the model client.

Offline checks cover the JSON extractor against the exact malformations observed
from real models. Live checks make three cheap calls (well under a cent total)
to confirm the transport, cost accounting and truncation handling work against
the real endpoint; pass --offline to skip them.

    python3 auditor/test_llm.py
    python3 auditor/test_llm.py --offline
"""

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from llm import extract_json, chat, load_env, LLMError, DEFAULT_MODEL, ESCALATION_MODEL  # noqa: E402

results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))


def offline_checks():
    check("plain object", extract_json('{"a": 1}') == {"a": 1})

    # kimi-k2.7-code prefixes its output with a space.
    check("leading whitespace", extract_json('  {"a": 1}') == {"a": 1})

    # Fenced output is the most common wrapper.
    check("markdown fence", extract_json('```json\n{"a": 1}\n```') == {"a": 1})
    check("bare fence", extract_json('```\n{"a": 1}\n```') == {"a": 1})

    # Narration before or after the object.
    check("prose prefix", extract_json('Here is the result:\n{"a": 1}') == {"a": 1})
    check("prose suffix", extract_json('{"a": 1}\nHope that helps!') == {"a": 1})

    # A brace inside a string must not end the scan early - this is the case a
    # naive rfind("}") gets wrong.
    check("brace inside string",
          extract_json('{"note": "use {braces} here", "a": 1}') == {"note": "use {braces} here", "a": 1})
    check("escaped quote in string",
          extract_json('{"note": "he said \\"hi\\"", "a": 2}') == {"note": 'he said "hi"', "a": 2})

    # Nested structure of the shape the auditor actually returns.
    payload = '{"findings": [{"path": "/payouts", "method": "post", "detail": "{fee}"}]}'
    check("nested findings", extract_json(payload)["findings"][0]["detail"] == "{fee}")

    # Failure must be None, never an exception and never a wrong guess.
    check("unparseable returns None", extract_json("no json here at all") is None)
    check("empty returns None", extract_json("") is None)
    check("truncated object returns None", extract_json('{"a": 1') is None)

    # Deliberately not asserted here. CI runs the offline suite with no key, and
    # a check that fails simply because a secret is absent trains people to
    # ignore a red build. Credentials are exercised by the live suite instead.
    env = load_env()
    if not (env.get("OPENROUTER_API_KEY") or env.get("OPENAI_API_KEY")):
        print("  note   no API key configured, live checks unavailable\n")


def live_checks():
    total_cost = 0.0

    reply = chat(DEFAULT_MODEL, "You return only JSON.",
                 'Return exactly {"ok": true} and nothing else.')
    total_cost += reply.cost_usd
    check("default model responds", reply.json is not None, reply.content[:80])
    check("default model reports cost", reply.cost_usd > 0, str(reply.cost_usd))
    check("default model reports tokens", reply.input_tokens > 0 and reply.output_tokens > 0)
    check("transport is fast (<60s)", reply.elapsed < 60, f"{reply.elapsed:.1f}s")

    # A reasoning model given no room to think returns empty content with
    # finish_reason=length. The client must flag that rather than report it as
    # a model that answered badly.
    tight = chat(DEFAULT_MODEL, "", 'Return exactly {"ok": true}', max_tokens=16)
    total_cost += tight.cost_usd
    check("truncation is detected", tight.truncated or tight.json is not None,
          f"finish={tight.finish_reason} content={tight.content[:40]!r}")

    escalation = chat(ESCALATION_MODEL, "You return only JSON.",
                      'Return exactly {"ok": true} and nothing else.')
    total_cost += escalation.cost_usd
    check("escalation model responds", escalation.json is not None, escalation.content[:80])

    # A model that does not exist must fail loudly, not silently return nothing.
    try:
        chat("definitely/not-a-real-model", "", "hi", retries=0)
        check("unknown model raises", False, "no exception raised")
    except LLMError:
        check("unknown model raises", True)

    print(f"\n  live checks spent ${total_cost:.6f}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="skip calls to the live endpoint")
    args = parser.parse_args()

    offline_checks()
    if not args.offline:
        live_checks()

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
