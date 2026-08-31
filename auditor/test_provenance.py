#!/usr/bin/env python3
"""Verification for what a report claims about its own provenance.

Four questions a reader asks of a finding, each of which this tool once got
wrong on a real repository, and each of which is invisible to the rule tests
because the rules themselves were right:

    which program is this about   a repository holding four applications was
                                  collapsed into one route table, and every
                                  finding was attributed to whichever file
                                  sorted first
    could the two sides ever      an undocumented 5xx against a generated
    have agreed                   specification is a permanent false positive:
                                  the generator cannot emit one
    did somebody already decide   a route left out of the specification on
    this                          purpose had nowhere to say so beside itself
    what actually ran             a brief with no confirmed verdict reported
                                  "this run had no API key" on runs that had one

The fixture is built here rather than checked in: its whole point is a shape -
several applications, one of them a narrow schema-publishing sub-application -
and a shape is clearer as fifteen lines of source than as a directory tree.

    python3 auditor/test_provenance.py
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))

import brief  # noqa: E402
from diff import audit, marked_internal, select_app  # noqa: E402
from spec import load as load_spec  # noqa: E402

import languages  # noqa: E402

# One repository, four applications. server/ holds the production application
# and, beside it, the narrow one that exists only to publish the schema
# integrators are given - the shape openai/chatgpt-retrieval-plugin has, and the
# one that made every earlier finding point at the wrong file.
SERVER = '''\
from fastapi import FastAPI, HTTPException

app = FastAPI()

sub_app = FastAPI(
    title="Retrieval Plugin API",
    description="A retrieval API for querying documents",
    version="1.0.0",
    servers=[{"url": "https://example.invalid"}],
)

@app.post("/query")
async def query_main(request):
    return {"results": []}

@app.post("/upsert")
async def upsert_main(request):
    return {"ids": []}

@sub_app.post("/query")
async def query_sub(request):
    try:
        return {"results": []}
    except Exception:
        raise HTTPException(status_code=500, detail="Internal Service Error")

# contract: internal
@sub_app.post("/reindex")
async def reindex_sub(request):
    return {"ok": True}

@sub_app.delete("/documents")
async def delete_sub(request):
    return {"ok": True}
'''

LOCAL = '''\
from fastapi import FastAPI

app = FastAPI()

@app.post("/query")
async def query(request):
    return {"results": []}
'''

EXAMPLE = '''\
from fastapi import FastAPI

app = FastAPI()

@app.post("/query")
async def query(request):
    return {"results": []}
'''

# FastAPI's own output: the operationId is {function}_{path}_{method}, and no
# 5xx appears anywhere because the generator never writes one it was not told
# about.
GENERATED_SPEC = {
    "openapi": "3.0.2",
    "info": {"title": "Retrieval Plugin API",
             "description": "A retrieval API for querying documents",
             "version": "1.0.0"},
    "servers": [{"url": "https://example.invalid"}],
    "paths": {"/query": {"post": {
        "operationId": "query_query_post",
        "requestBody": {"content": {"application/json": {
            "schema": {"type": "object", "properties": {"q": {"type": "string"}}}}}},
        "responses": {"200": {"description": "ok", "content": {"application/json": {
            "schema": {"type": "object",
                       "properties": {"results": {"type": "array"}}}}}}}}}},
}

results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))


def build(root):
    for relative, source in (("server/main.py", SERVER),
                             ("local_server/main.py", LOCAL),
                             ("examples/no-auth/main.py", EXAMPLE)):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)

    generated = root / "openapi.json"
    generated.write_text(json.dumps(GENERATED_SPEC, indent=2))

    # The same document with the generator's fingerprint removed, which is what
    # a hand-written specification looks like to the status rules.
    handwritten = json.loads(json.dumps(GENERATED_SPEC))
    del handwritten["paths"]["/query"]["post"]["operationId"]
    (root / "handwritten.json").write_text(json.dumps(handwritten, indent=2))
    return generated, root / "handwritten.json"


def main():
    with tempfile.TemporaryDirectory() as temporary:
        root = pathlib.Path(temporary)
        generated, handwritten = build(root)

        table = languages.get("python").extract(root, strip_prefix="")
        registrations = {(r["file"], r["line"]) for r in table["routes"]}
        check("every registration survives extraction", len(registrations) == 7,
              f"got {len(registrations)}")
        check("routes are keyed by application, not by path alone",
              len([r for r in table["routes"] if r["path"] == "/query"]) == 4,
              "the same path in four applications is four routes")

        spec = load_spec(generated)
        app_id, why = select_app(table, spec)
        check("the spec is matched to the application that generated it",
              app_id.endswith("::sub_app"), f"got {app_id!r}")
        check("the match names the evidence behind it",
              "title" in why and "version" in why, why)

        coverage = {}
        findings = audit(root, generated, strip_prefix="", language="python",
                         coverage=coverage)
        kinds = sorted((f["kind"], f["path"]) for f in findings)

        check("only the matched application's routes are audited",
              coverage["registrations"] == 3, f"got {coverage['registrations']}")
        check("the narrowing is stated, not silent",
              bool(coverage.get("narrowed_to_app")))
        check("a route in an unmatched application raises nothing",
              not any(p in ("/upsert",) for _, p in kinds), kinds)

        check("the generator is recognised", coverage["generated_by"] == "fastapi")
        check("an undocumented 5xx is not reported against generated output",
              ("undocumented_status", "/query") not in kinds, kinds)
        check("the suppression is counted rather than hidden",
              coverage["suppressed_generated_status"] == 1,
              f"got {coverage['suppressed_generated_status']}")

        hand = {}
        audit(root, handwritten, strip_prefix="", language="python", coverage=hand)
        check("the same 5xx is still reported against a hand-written spec",
              hand["suppressed_generated_status"] == 0 and not hand["generated_by"])

        check("a marked route is left out of the undocumented findings",
              ("route_missing_from_spec", "/reindex") not in kinds, kinds)
        check("the marker is counted", coverage["marked_internal"] == 1,
              f"got {coverage['marked_internal']}")
        check("an unmarked route beside it is still reported",
              ("route_missing_from_spec", "/documents") in kinds, kinds)

        # The marker covers the registration it sits above and nothing else. A
        # fixed lookback reached past a short handler into the previous route,
        # which silently excluded an endpoint nobody had marked.
        following = next(r for r in table["routes"] if r["path"] == "/documents")
        check("the marker does not reach the next registration",
              not marked_internal(root, following))

    # What the brief says ran. These are the two facts that were rendered as
    # one: a key was supplied and the gate did not run is not "no API key".
    with_key = brief.how_it_was_established(
        [{"kind": "undocumented_status"}],
        {"layers": {"agent": True, "gate": False}, "agent_claims": 0,
         "endpoints_judged": 3, "calls": 3})
    check("a run with a key is never described as having none",
          "no API key" not in with_key, with_key)
    check("a judgment pass that added nothing says so",
          "added nothing" in with_key, with_key)
    check("an unverified run says the gate did not run",
          "gate did not run" in with_key, with_key)

    without_key = brief.how_it_was_established(
        [{"kind": "undocumented_status"}],
        {"layers": {"agent": False, "gate": False}})
    check("a run with no key says so plainly", "no API key" in without_key,
          without_key)

    verified = brief.how_it_was_established(
        [{"kind": "undocumented_status", "verdict": "confirmed"}],
        {"layers": {"agent": True, "gate": True}, "agent_claims": 1})
    check("an executed finding is reported as executed",
          "proved by executing" in verified, verified)

    # The closing instruction, which used to send every reader to a tests/
    # directory whether or not one had been written.
    empty = brief.render([{"path": "/x", "method": "get", "severity": "medium",
                           "kind": "undocumented_status"}], "audit", {})
    check("a brief with no tests does not tell the reader to run tests",
          "Run the tests in `tests/`" not in empty and "ships no tests" in empty)

    shipped = brief.render(
        [{"path": "/x", "method": "get", "severity": "medium",
          "kind": "undocumented_status", "verdict": "confirmed",
          "test_source": "assert True", "test_filename": "x_check.py"}],
        "audit", {"layers": {"agent": True, "gate": True}})
    check("a brief with tests names the files it shipped",
          "`tests/x_check.py`" in shipped)

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
