#!/usr/bin/env python3
"""Write a contract-audit workflow by reading the repository.

The three inputs that differ between projects - where the source is, which spec
to audit it against, which prefix to strip - are exactly the three a newcomer to
a codebase cannot answer, and the fourth, which middleware defines the contract,
is worse: it needs knowing that `authenticate` guards the merchant API while
`adminAuthenticate` guards the console. Answering those by reading the code is
the barrier this exists to remove.

Nothing here guesses. Each value is derived and each derivation is written into
the generated file as a comment, so the output is reviewable rather than magic:

    language        the marker file that decided it
    spec            the candidate with the most documented operations
    strip-prefix    the path component of the spec's own servers[].url
    source-dir      the candidate directory that yielded the most routes
    contract-middleware
                    the spec names the credential integrators were promised, in
                    components.securitySchemes. The middleware whose source
                    reads that header is the guard that defines the contract.
                    Everything else - a console's session guard, an unguarded
                    internal route - is outside it.

    python3 auditor/init.py --repo .    # writes .github/workflows/contract-audit.yml
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "tools"))

import languages  # noqa: E402
from diff import auth_header_names  # noqa: E402
from spec import load as load_spec  # noqa: E402

# Directories that never hold a project's own route registrations, and are large
# enough that walking them turns a two second scan into a minute.
SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", "__pycache__",
             ".venv", "venv", ".next", "target", "coverage", ".mypy_cache"}

# Where a GitHub workflow has to live to run at all. init writes it there
# rather than into the working directory, because a file called
# contract-audit.yml in the repository root does nothing whatsoever.
WORKFLOW_PATH = ".github/workflows/contract-audit.yml"

SPEC_NAMES = ("openapi.json", "openapi.yaml", "openapi.yml",
              "swagger.json", "swagger.yaml", "swagger.yml")
SPEC_DIRS = (".", "docs", "api", "spec", "mintlify", "public", "static",
             "openapi", "doc")

# Ordered by how likely each is to be the directory that registers routes. The
# first that yields routes wins ties, so a project with both src and app gets
# the conventional answer for its language rather than the alphabetical one.
SOURCE_CANDIDATES = {
    "go": ("internal", "cmd", "handlers", "api", "."),
    "typescript": ("src", "app", "lib", "routes", "."),
    "python": ("app", "src", "api", "."),
    "php": (".", "routes", "app"),
}


def walk(root):
    """Every file under root, skipping the directories that are never source."""
    for path in root.rglob("*"):
        if path.is_dir() or any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def find_specs(root):
    """Candidate OpenAPI documents, richest first.

    Richest rather than first-found: a repository often carries a stub spec next
    to the real one, and the one describing more operations is the one anybody
    would have meant.
    """
    seen, out = set(), []
    for directory in SPEC_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for name in SPEC_NAMES:
            path = base / name
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            try:
                spec = load_spec(path)
                operations = len(spec.keys())
            except Exception:
                continue
            if operations:
                out.append((operations, path, spec))
    # Anything not in a conventional directory, so a spec at mintlify/v2/ or
    # docs/reference/ is still found rather than silently skipped.
    if not out:
        for path in walk(root):
            if path.name in SPEC_NAMES and path not in seen:
                try:
                    spec = load_spec(path)
                    operations = len(spec.keys())
                except Exception:
                    continue
                if operations:
                    out.append((operations, path, spec))
    return sorted(out, key=lambda row: -row[0])


def server_prefix(spec):
    """The path component of the spec's first server URL, e.g. /api/v1.

    This is the prefix the code registers and the spec's paths leave out, which
    is what strip-prefix exists to reconcile.
    """
    for server in spec.document.get("servers", []) or []:
        url = str(server.get("url", ""))
        path = re.sub(r"^https?://[^/]+", "", url).rstrip("/")
        if path and path != "/":
            return path
    return ""


def route_table(adapter, directory, prefix):
    try:
        table = adapter.extract(directory, strip_prefix=prefix)
    except Exception:
        return None
    return table if (table or {}).get("routes") else None


def pick_source(root, adapter, prefix):
    """The candidate directory that yields the most routes.

    Most rather than first: `.` almost always extracts something, and picking it
    would drag a repository's outbound HTTP clients and test fixtures into the
    audit alongside its real handlers.
    """
    best = None
    for candidate in SOURCE_CANDIDATES.get(adapter.NAME, (".",)):
        directory = (root / candidate).resolve()
        if not directory.is_dir():
            continue
        table = route_table(adapter, directory, prefix)
        if not table:
            continue
        count = len(table["routes"])
        if best is None or count > best[0]:
            best = (count, candidate, table)
    return best


# A middleware has to be exported to be imported by the file that registers the
# route, and it has to be declared at the top level to be exported. Both halves
# matter: without the export test, `const payload = jwt.verify(...)` inside a
# session guard reads as a middleware named `payload`, because it sits in a file
# that does mention the header.
DEFINITION = (
    r"^export\s+(?:default\s+)?(?:async\s+)?(?:function|const|let|var|class)\s+{name}\b"
    r"|^export\s*\{{[^}}]*\b{name}\b[^}}]*\}}"
    r"|^(?:module\.)?exports\.{name}\s*="
    r"|^def\s+{name}\b"
    r"|^func\s+(?:\([^)]*\)\s*)?{name}\b"
)


def contract_guards(root, table, headers):
    """Middleware whose source reads a credential the spec promises integrators.

    The spec is the authority on what the contract's credential is: an apiKey
    security scheme names the header, and http auth means Authorization. A file
    that reads that header and defines a middleware the routes use is the guard
    that separates the promised API from everything else in the same codebase.
    """
    used = sorted({m for route in table["routes"] for m in (route.get("middleware") or [])})
    if not used or not headers:
        return [], used

    # Authorization is worth almost nothing on its own: a session guard reads it
    # too. It only discriminates when the spec offers no better credential.
    strong = {h for h in headers if h.lower() != "authorization"}
    wanted = strong or headers

    candidates = []
    for path in walk(root):
        if path.suffix.lower() not in (".ts", ".js", ".mjs", ".tsx", ".go", ".py", ".php"):
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        if any(header.lower() in text.lower() for header in wanted):
            candidates.append(text)

    guards = []
    for name in used:
        bare = name[:-2] if name.endswith("()") else name
        if not bare.isidentifier():
            continue
        pattern = re.compile(DEFINITION.format(name=re.escape(bare)), re.M)
        if any(pattern.search(text) for text in candidates):
            guards.append(bare)
    return guards, used


def comment(text, indent="          "):
    """A reason, wrapped. A derivation worth writing down is worth reading, and
    a 200 character line in a workflow file is not read by anybody."""
    import textwrap
    return [f"{indent}# {line}" for line in
            textwrap.wrap(text, width=72 - len(indent)) or [""]]


def render(findings):
    """The workflow file. Every derived value carries the reason beside it."""
    f = findings
    lines = [
        "name: Contract Audit",
        "",
        "# Generated by contract-auditor init. Every value below was derived from",
        "# this repository, and the reason is beside it. Read them once: a wrong",
        "# spec or source-dir produces a report about the wrong thing rather than",
        "# an error.",
        "",
        "on:",
        "  pull_request:",
        "  push:",
        f"    branches: [{f['default_branch']}]",
        "  schedule:",
        "    # contract-auditor@v1 is a floating tag, so new rules can find drift",
        "    # in code that has not changed. Delete this if that is not worth a run.",
        "    - cron: '0 9 1 * *'",
        "  workflow_dispatch:",
        "",
        "permissions:",
        "  contents: read",
        "  pull-requests: write",
        "",
        "jobs:",
        "  contract-audit:",
        "    name: API contract drift",
        "    runs-on: ubuntu-latest",
        "",
        "    steps:",
        "      - uses: actions/checkout@v4",
        "",
        "      - name: Audit code against the OpenAPI spec",
        "        id: audit",
        "        uses: samso9th/contract-auditor@v1",
        "        # Report only to start with. Tighten fail-on to critical, then",
        "        # high, once the first backlog is cleared.",
        "        continue-on-error: true",
        "        with:",
        *comment(f["spec_why"]),
        f"          spec: {f['spec']}",
        *comment(f["source_why"]),
        f"          source-dir: {f['source_dir']}",
        f"          language: {f['language']}",
    ]
    if f["strip_prefix"]:
        lines += comment(f["prefix_why"]) + [
            f"          strip-prefix: {f['strip_prefix']}"]
    else:
        lines += ["          # No servers[].url path in the spec, so nothing to strip.",
                  "          # If the first run reports every route as missing from the",
                  "          # spec, this is the input that is wrong."]

    if f["guards"]:
        lines += comment(f["guard_why"]) + [
            "          contract-middleware: " + ", ".join(f["guards"])]
    else:
        lines += comment("contract-middleware not set: " + f["guard_why"]) + [
            "          # exclude-paths: |",
            "          #   /admin/*",
            "          #   /internal/*"]

    lines += [
        "          # Optional. Without it only the deterministic layer runs: no",
        "          # model calls, no cost, and it still catches most drift.",
        "          api-key: ${{ secrets.OPENROUTER_API_KEY }}",
        "          fail-on: none",
        "",
        "      - name: Upload the fix brief",
        "        id: brief",
        "        uses: actions/upload-artifact@v4",
        "        if: always() && steps.audit.outputs.brief-dir != ''",
        "        with:",
        "          name: contract-audit-brief",
        "          path: ${{ steps.audit.outputs.brief-dir }}",
        "          if-no-files-found: ignore",
        "",
        "      # The audit runs as root in a container, so the summary it writes",
        "      # belongs to root. Copy before appending: the copy belongs to the",
        "      # runner. The artifact id does not exist until the upload above has",
        "      # finished, which is why the link cannot come from the audit step.",
        "      - name: Build the comment body",
        "        id: body",
        "        if: always() && steps.audit.outputs.summary != ''",
        "        env:",
        "          SUMMARY: ${{ steps.audit.outputs.summary }}",
        "          BRIEF_URL: ${{ steps.brief.outputs.artifact-url }}",
        "        run: |",
        "          set -euo pipefail",
        "          cp \"$SUMMARY\" contract-audit-comment.md",
        "          [ -z \"$BRIEF_URL\" ] || \\",
        "            printf '\\n**[Download the fix brief](%s)**\\n' \"$BRIEF_URL\" "
        ">> contract-audit-comment.md",
        "          echo \"path=contract-audit-comment.md\" >> \"$GITHUB_OUTPUT\"",
        "",
        "      - name: Comment the summary on the pull request",
        "        uses: marocchino/sticky-pull-request-comment@v2",
        "        if: always() && github.event_name == 'pull_request' "
        "&& steps.body.outputs.path != ''",
        "        with:",
        "          header: contract-audit",
        "          path: ${{ steps.body.outputs.path }}",
        "",
    ]
    return "\n".join(lines)


def inspect(root, default_branch="main"):
    root = pathlib.Path(root).resolve()

    adapter = languages.detect(root)
    if adapter is None:
        raise SystemExit(
            f"could not identify a language in {root}. Supported: "
            f"{', '.join(languages.names())}. Point --repo at the directory "
            f"holding go.mod, package.json, pyproject.toml or artisan.")

    specs = find_specs(root)
    if not specs:
        raise SystemExit(
            f"no OpenAPI document found under {root}. Looked for "
            f"{', '.join(SPEC_NAMES)} in {', '.join(SPEC_DIRS)}. There is nothing "
            f"to audit code against until one exists.")
    operations, spec_path, spec = specs[0]

    prefix = server_prefix(spec)
    picked = pick_source(root, adapter, prefix)
    if picked is None:
        raise SystemExit(
            f"no routes extracted from any of "
            f"{', '.join(SOURCE_CANDIDATES.get(adapter.NAME, ('.',)))} in {root}. "
            f"The language was detected as {adapter.NAME}; if that is wrong, the "
            f"marker file that decided it is not this project's main language.")
    route_count, source_dir, table = picked

    headers = auth_header_names(spec)
    guards, used = contract_guards(root, table, headers)

    if guards:
        guard_why = (f"{', '.join(sorted(headers))} is the credential this spec "
                     f"promises integrators, and these read it. Routes behind any "
                     f"other guard are a different API and leave the audit.")
    elif not used:
        guard_why = (f"the {adapter.NAME} extractor records no route middleware, "
                     f"so the contract cannot be told from a console by its guard. "
                     f"Use exclude-paths instead:")
    elif not headers:
        guard_why = ("this spec declares no securitySchemes, so nothing names the "
                     "credential integrators were promised. Routes are guarded by "
                     f"{', '.join(used[:4])}; name the contract one by hand, or use:")
    else:
        guard_why = (f"none of {', '.join(used[:4])} reads "
                     f"{', '.join(sorted(headers))}. Name the contract guard by "
                     f"hand, or use:")

    return {
        "language": adapter.NAME,
        "spec": spec_path.relative_to(root).as_posix(),
        "spec_why": f"{operations} documented operation(s), the richest spec found.",
        "source_dir": source_dir,
        "source_why": f"{route_count} route(s) extracted here, more than any other "
                      f"candidate directory.",
        "strip_prefix": prefix,
        "prefix_why": "the path component of this spec's own servers[].url.",
        "guards": guards,
        "guard_why": guard_why,
        "middleware_seen": used,
        "routes": route_count,
        "operations": operations,
        "default_branch": default_branch,
    }


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".", help="the repository to read")
    parser.add_argument("--branch", default="main",
                        help="branch the push trigger watches")
    parser.add_argument("--out", default=WORKFLOW_PATH,
                        help=f"where to write the workflow (default {WORKFLOW_PATH}, "
                             f"relative to --repo)")
    parser.add_argument("--stdout", action="store_true",
                        help="print the workflow instead of writing it")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing workflow")
    parser.add_argument("--json", action="store_true",
                        help="emit what was detected, not a workflow")
    args = parser.parse_args()

    findings = inspect(args.repo, args.branch)
    if args.json:
        print(json.dumps(findings, indent=2, sort_keys=True))
        return

    workflow = render(findings)
    if args.stdout:
        print(workflow)
    else:
        # Relative to the repository being read, not the working directory: the
        # container runs with the repo mounted somewhere else entirely, and a
        # workflow written beside the container's cwd helps nobody.
        path = pathlib.Path(args.out)
        if not path.is_absolute():
            path = pathlib.Path(args.repo).resolve() / path
        if path.exists() and not args.force:
            raise SystemExit(
                f"{path} already exists. Read it, then pass --force to replace it, "
                f"or --stdout to print the generated one and merge by hand.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(workflow)
        print(f"wrote {path}", file=sys.stderr)

    # To stderr, so the workflow can be redirected into a file while a person
    # still sees what was decided.
    print(f"\ndetected {findings['language']}: {findings['routes']} route(s) in "
          f"{findings['source_dir']}, {findings['operations']} operation(s) in "
          f"{findings['spec']}", file=sys.stderr)
    if findings["guards"]:
        print(f"contract guard(s): {', '.join(findings['guards'])}", file=sys.stderr)
    elif findings["middleware_seen"]:
        print(f"middleware seen but not matched: "
              f"{', '.join(findings['middleware_seen'][:6])}", file=sys.stderr)


if __name__ == "__main__":
    main()
