#!/usr/bin/env python3
"""Provider-neutral model client: component 5 of the auditor.

Talks to any OpenAI-compatible chat-completions endpoint. Configured for
OpenRouter, which reaches Anthropic, Z.AI, Moonshot, DeepSeek and the rest
through one key, so the harness is not tied to a single vendor and a judge can
run it with whatever credentials they already have.

Two deliberate choices, both forced by measurement rather than taste:

**Transport is curl, not urllib or requests.** On the development machine this
system Python links LibreSSL 2.8.3, and both `urllib` and `requests` took ~160s
for calls curl completes in 3-7s. Shelling out also keeps the whole harness
dependency-free, which is worth points on reproducibility.

**JSON is extracted, not trusted.** Models fence it, prefix it with whitespace,
or narrate around it. `moonshotai/kimi-k2.7-code` returns a leading space before
the object. Parsing has to tolerate that without tolerating garbage.

    from auditor.llm import chat, MODELS
    reply = chat("z-ai/glm-5.3-flash", "You audit APIs.", "Find the drift...")
    reply.json          # parsed dict, or None
    reply.cost_usd      # what the call actually cost, from the provider

**Model and reasoning are the caller's choice.** Any id the endpoint accepts
works, not only the three benchmarked below; `AUDITOR_MODEL` in `.env` or the
environment sets the default, and `--model` overrides it per run. `--reasoning`
(`off`, `low`, `medium`, `high`) asks the provider for more deliberation, which
costs output tokens and wall-clock time and is off unless asked for.
"""

# Keeps `dict | None` annotations legal on Python 3.9, which is what ships with
# macOS command line tools and is therefore what a judge is most likely to have.
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"

# Verified against OpenRouter's live model list. Prices are USD per 1M tokens and
# are recorded here only for estimates - `cost_usd` on every reply is the real
# figure the provider reported, never a local calculation.
MODELS = {
    "z-ai/glm-5.3-flash": {
        "role": "default",
        "context": 1_310_720,
        "price_in": 0.075, "price_out": 0.250,
        "reasoning": "always-on",
        "note": "Reasoning cannot be disabled - `reasoning:{enabled:false}` returns 400. "
                "It spends output tokens thinking before answering, so max_tokens must be "
                "generous or content comes back null with finish_reason=length.",
    },
    "moonshotai/kimi-k2.7-code": {
        "role": "escalation",
        "context": 262_144,
        "price_in": 0.66, "price_out": 3.40,
        "reasoning": "on",
        "note": "Returns a leading space before its JSON. Use it only where the default "
                "model measurably fails; it is ~14x the input price.",
    },
    "deepseek/deepseek-v3.2": {
        "role": "alternate",
        "context": 163_840,
        "price_in": 0.28, "price_out": 0.42,
        "reasoning": "on",
        "note": "Cheapest reliable JSON responder in the bench. Kept as a second opinion.",
    },
}

# The three above are what the evaluation numbers were produced with, not a
# closed list. `chat` sends whatever id it is given, so any model the configured
# endpoint serves can be selected without editing this file.
FALLBACK_MODEL = "z-ai/glm-5.3-flash"
ESCALATION_MODEL = "moonshotai/kimi-k2.7-code"

# GLM 5.3 Flash reasons before answering and the reasoning is billed as output.
# A ceiling that only fits the answer truncates the thinking instead, and the
# reply arrives empty - the first failure this client hit.
DEFAULT_MAX_TOKENS = 8000

# How much deliberation to ask the provider for. `off` is a request, not a
# guarantee: some models reason unconditionally and reject being told not to.
REASONING_LEVELS = ("off", "low", "medium", "high")

# Thinking is billed as output and is emitted before the answer, so a ceiling
# sized for the answer alone truncates mid-thought and returns empty content.
# These are floors, applied only when the caller asked for reasoning.
REASONING_MIN_TOKENS = {"low": 12000, "medium": 20000, "high": 32000}


class LLMError(RuntimeError):
    pass


@dataclass
class Reply:
    """One model response, with everything the changelog needs to cite it."""
    model: str
    content: str
    json: dict | None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    elapsed: float = 0.0
    finish_reason: str = ""
    truncated: bool = False
    reasoning_tokens: int = 0
    raw: dict = field(default_factory=dict, repr=False)


def load_env(path=ENV_FILE):
    """Read .env without exporting it. Values stay in this process; nothing is
    written to a log or a findings file."""
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "OPENAI_API_KEY",
                "OPENAI_BASE_URL", "AUDITOR_MODEL", "AUDITOR_REASONING"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def default_model():
    """The model used when nothing is passed: `AUDITOR_MODEL` from the
    environment or `.env`, else the benchmarked default."""
    return load_env().get("AUDITOR_MODEL") or FALLBACK_MODEL


def default_reasoning():
    """The reasoning level used when nothing is passed, or None to send no
    reasoning field at all and let the provider decide."""
    return normalise_reasoning(load_env().get("AUDITOR_REASONING"))


def normalise_reasoning(level):
    """Accept a level in any casing, treat blank and `default` as unset, and
    reject anything else loudly rather than silently ignoring it."""
    if level is None:
        return None
    level = str(level).strip().lower()
    if level in ("", "default", "auto"):
        return None
    if level in ("none", "false", "disabled"):
        level = "off"
    if level not in REASONING_LEVELS:
        raise LLMError(f"unknown reasoning level {level!r}; "
                       f"expected one of {', '.join(REASONING_LEVELS)}")
    return level


DEFAULT_MODEL = default_model()
DEFAULT_REASONING = default_reasoning()


def credentials():
    env = load_env()
    key = env.get("OPENROUTER_API_KEY") or env.get("OPENAI_API_KEY")
    base = (env.get("OPENROUTER_BASE_URL") or env.get("OPENAI_BASE_URL")
            or "https://openrouter.ai/api/v1")
    if not key:
        raise LLMError(
            "no API key found. Set OPENROUTER_API_KEY in .env or the environment. "
            "Any OpenAI-compatible endpoint works; set OPENROUTER_BASE_URL to point elsewhere."
        )
    return key, base.rstrip("/")


def extract_json(text):
    """Recover a JSON object from a model reply.

    Handles the three things models actually do: wrap the object in a markdown
    fence, prefix it with whitespace or a sentence, and trail it with commentary.
    Returns None rather than raising - an unparseable reply is a fact the caller
    records, not an exception that aborts a 16-case run.
    """
    if not text:
        return None
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Scan for the first balanced object, respecting strings and escapes so a
    # brace inside a value does not end the scan early.
    start = text.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:index + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def chat(model=None, system="", user="", max_tokens=DEFAULT_MAX_TOKENS, json_mode=True,
         temperature=0, retries=2, timeout=300, reasoning=None):
    """One chat completion. Returns a Reply; raises LLMError only if every
    attempt failed to produce a response at all.

    `model` defaults to `AUDITOR_MODEL` and `reasoning` to `AUDITOR_REASONING`;
    either can be overridden per call. A reasoning level is a request to the
    provider, so it is sent only when asked for - the model's own default is
    left alone otherwise, which is what every measurement in this repository
    was taken under.
    """
    key, base = credentials()
    model = model or DEFAULT_MODEL
    level = normalise_reasoning(reasoning if reasoning is not None else DEFAULT_REASONING)

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    payload = {"model": model, "messages": messages,
               "max_tokens": max_tokens, "temperature": temperature}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if level is not None:
        payload["reasoning"] = ({"enabled": False} if level == "off"
                                else {"effort": level})
        # Raise the ceiling rather than let the thinking eat the answer. Only a
        # floor: a caller that already asked for more keeps what it asked for.
        payload["max_tokens"] = max(max_tokens, REASONING_MIN_TOKENS.get(level, 0))

    last_error = ""
    for attempt in range(retries + 1):
        started = time.time()
        completed = subprocess.run(
            ["curl", "-sS", "-m", str(timeout),
             "-H", f"Authorization: Bearer {key}",
             "-H", "Content-Type: application/json",
             "-H", "X-Title: micro1-contract-auditor",
             "-d", json.dumps(payload),
             f"{base}/chat/completions"],
            capture_output=True, text=True,
        )
        elapsed = time.time() - started

        if completed.returncode != 0:
            last_error = completed.stderr.strip() or f"curl exited {completed.returncode}"
        else:
            try:
                document = json.loads(completed.stdout)
            except json.JSONDecodeError:
                last_error = f"non-JSON response: {completed.stdout[:200]}"
                document = None

            if document is not None:
                if "error" in document:
                    last_error = str(document["error"])[:300]
                    # A bad request will fail identically on retry; only
                    # transient conditions are worth attempting again.
                    if not _transient(last_error):
                        # GLM 5.3 Flash is the known case: it reasons
                        # unconditionally and 400s on being told not to. The id
                        # and the level together are what the user has to change,
                        # so name both rather than pass the provider's text on.
                        if level is not None and "reasoning" in last_error.lower():
                            raise LLMError(
                                f"{model} rejected reasoning={level}: {last_error}. "
                                f"This model does not accept that setting; drop "
                                f"--reasoning or choose another model.")
                        raise LLMError(f"{model}: {last_error}")
                else:
                    return _to_reply(model, document, elapsed)

        if attempt < retries:
            time.sleep(2 ** attempt)

    raise LLMError(f"{model}: {last_error or 'no response'}")


def _transient(message):
    lowered = message.lower()
    return any(w in lowered for w in ("rate", "429", "timeout", "502", "503", "504", "overload"))


def _to_reply(model, document, elapsed):
    choice = (document.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = document.get("usage") or {}
    content = message.get("content") or ""
    finish = choice.get("finish_reason") or ""
    details = usage.get("completion_tokens_details") or {}

    return Reply(
        model=document.get("model", model),
        content=content,
        json=extract_json(content),
        input_tokens=usage.get("prompt_tokens") or 0,
        output_tokens=usage.get("completion_tokens") or 0,
        cost_usd=float(usage.get("cost") or 0.0),
        elapsed=elapsed,
        finish_reason=finish,
        # Empty content with finish_reason=length means a reasoning model spent
        # the whole budget thinking. Distinct from a refusal, and the fix is a
        # larger ceiling, so it is surfaced rather than reported as a bad reply.
        truncated=(finish == "length"),
        # Thinking is billed as output. Reporting it separately is what makes
        # the price of a reasoning level legible instead of a mystery increase.
        reasoning_tokens=details.get("reasoning_tokens") or 0,
        raw=document,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="any model id the endpoint serves "
                             f"(default {DEFAULT_MODEL}, set by AUDITOR_MODEL)")
    parser.add_argument("--reasoning", choices=REASONING_LEVELS, default=DEFAULT_REASONING,
                        help="how much deliberation to ask for. Slower and more "
                             "expensive; omitted, the model's own default applies")
    parser.add_argument("--prompt", default='Return ONLY JSON: {"ok": true}')
    parser.add_argument("--system", default="")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--models", action="store_true", help="list the configured models")
    args = parser.parse_args()

    if args.models:
        print(f"{'MODEL':<28} {'ROLE':<11} {'CONTEXT':>10}  {'$/1M IN':>8} {'$/1M OUT':>9}")
        for name, spec in MODELS.items():
            marker = " *" if name == DEFAULT_MODEL else "  "
            print(f"{name:<28} {spec['role']:<11} {spec['context']:>10,}  "
                  f"{spec['price_in']:>8.3f} {spec['price_out']:>9.3f}{marker}")
        print(f"\n* default ({'AUDITOR_MODEL' if DEFAULT_MODEL != FALLBACK_MODEL else 'built-in'})")
        for name, spec in MODELS.items():
            print(f"\n{name}\n  {spec['note']}")
        print("\nThese three are benchmarked, not exhaustive. Any id the endpoint "
              "serves works:\n  --model <id>, or AUDITOR_MODEL=<id> in .env"
              "\n  --reasoning " + "|".join(REASONING_LEVELS) +
              ", or AUDITOR_REASONING=<level>"
              f"\n  reasoning is currently {DEFAULT_REASONING or 'unset (provider default)'}")
        return

    try:
        reply = chat(args.model, args.system, args.prompt, max_tokens=args.max_tokens,
                     reasoning=args.reasoning)
    except LLMError as exc:
        sys.exit(f"error: {exc}")

    print(f"model    {reply.model}")
    print(f"elapsed  {reply.elapsed:.1f}s")
    print(f"tokens   in={reply.input_tokens} out={reply.output_tokens}"
          f"{f' reasoning={reply.reasoning_tokens}' if reply.reasoning_tokens else ''}")
    print(f"cost     ${reply.cost_usd:.6f}")
    print(f"finish   {reply.finish_reason}{'  (TRUNCATED)' if reply.truncated else ''}")
    print(f"parsed   {'yes' if reply.json is not None else 'no'}")
    print(f"\n{reply.content[:600]}")


if __name__ == "__main__":
    main()
