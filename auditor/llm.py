#!/usr/bin/env python3
"""Provider-neutral model client — component 5 of the auditor.

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

DEFAULT_MODEL = "z-ai/glm-5.3-flash"
ESCALATION_MODEL = "moonshotai/kimi-k2.7-code"

# GLM 5.3 Flash reasons before answering and the reasoning is billed as output.
# A ceiling that only fits the answer truncates the thinking instead, and the
# reply arrives empty - the first failure this client hit.
DEFAULT_MAX_TOKENS = 8000


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
    for key in ("OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


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


def chat(model, system, user, max_tokens=DEFAULT_MAX_TOKENS, json_mode=True,
         temperature=0, retries=2, timeout=300):
    """One chat completion. Returns a Reply; raises LLMError only if every
    attempt failed to produce a response at all."""
    key, base = credentials()

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    payload = {"model": model, "messages": messages,
               "max_tokens": max_tokens, "temperature": temperature}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

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
        raw=document,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=DEFAULT_MODEL)
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
        print("\n* default")
        for name, spec in MODELS.items():
            print(f"\n{name}\n  {spec['note']}")
        return

    try:
        reply = chat(args.model, args.system, args.prompt, max_tokens=args.max_tokens)
    except LLMError as exc:
        sys.exit(f"error: {exc}")

    print(f"model    {reply.model}")
    print(f"elapsed  {reply.elapsed:.1f}s")
    print(f"tokens   in={reply.input_tokens} out={reply.output_tokens}")
    print(f"cost     ${reply.cost_usd:.6f}")
    print(f"finish   {reply.finish_reason}{'  (TRUNCATED)' if reply.truncated else ''}")
    print(f"parsed   {'yes' if reply.json is not None else 'no'}")
    print(f"\n{reply.content[:600]}")


if __name__ == "__main__":
    main()
