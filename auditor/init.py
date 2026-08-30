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
    source-dir      of the directories this repository declares a unit - a
                    conventional source root, or anything carrying its own
                    dependency manifest - the one implementing the most of the
                    spec with the fewest routes it never mentions. Not the one
                    with the most routes: on a repository holding several
                    services that is always the root, because it is the union
                    of all of them.
    contract-middleware
                    the spec names the credential integrators were promised, in
                    components.securitySchemes. Of the middleware whose source
                    reads that header, the contract guard is the one protecting
                    the endpoints the spec documents. Everything else - a
                    console's session guard, an unguarded internal route - is
                    outside it.

    python3 auditor/init.py --repo .    # writes .github/workflows/contract-audit.yml
"""

from __future__ import annotations

import argparse
import difflib
import json
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "tools"))

import languages  # noqa: E402
from diff import auth_header_names, guards_reading  # noqa: E402
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

# The file a language's toolchain requires at the root of anything it can build
# or install. A name list cannot find a service directory - server/, apps/api/
# and backend/ are the same thing and none of them is a convention anyone
# agreed on - but a manifest is not a convention. The toolchain will not work
# without it, so it is there whatever the directory is called.
SERVICE_MANIFESTS = {
    "go": ("go.mod",),
    "typescript": ("package.json",),
    "python": ("pyproject.toml", "setup.py", "Pipfile", "requirements.txt"),
    "php": ("composer.json",),
}

# How deep to look for one. Deep enough for apps/api and services/gateway/api,
# shallow enough that the scan stays instant on a large repository.
MANIFEST_DEPTH = 4


# Branches a team treats as shared, and so wants audited on their merged result
# rather than only on the pull requests into them. Anything else is somebody's
# feature branch, which the pull_request trigger already covers.
SHARED_BRANCHES = ("main", "master", "develop", "dev", "staging", "stage",
                   "production", "prod")


def remote_branches(root):
    """Branch names from the remote itself, empty when it cannot be reached.

    Asked of the remote rather than read from .git because a clone knows only
    what it fetched: --depth 1, --single-branch, and actions/checkout all leave
    exactly one remote ref behind, and a workflow generated from that would
    silently miss the branch the team actually deploys from.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "-c", "safe.directory=*",
             "ls-remote", "--heads", "origin"],
            capture_output=True, text=True, timeout=20, check=True).stdout
    except Exception:
        return set()          # no network, no remote, no credentials, no git
    return {line.rsplit("refs/heads/", 1)[-1].strip()
            for line in out.splitlines() if "refs/heads/" in line}


def git_branches(root):
    """The default branch and the shared branches this project has.

    Read rather than asked for, because the person running init is usually not
    the person who knows whether this project deploys from `dev` or `staging`.
    A repository with no git at all yields main, which is the safe guess.
    """
    git = root / ".git"
    default, present = "", set()

    head = git / "refs" / "remotes" / "origin" / "HEAD"
    if head.is_file():
        default = head.read_text().strip().rsplit("/", 1)[-1]

    remotes = git / "refs" / "remotes" / "origin"
    if remotes.is_dir():
        present |= {p.name for p in remotes.iterdir() if p.is_file() and p.name != "HEAD"}
    packed = git / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(errors="ignore").splitlines():
            _, _, ref = line.partition(" refs/remotes/origin/")
            if ref and "/" not in ref:
                present.add(ref.strip())

    # The remote is the authority; the local refs are what is left when it
    # cannot be reached.
    present |= remote_branches(root)

    shared = [b for b in SHARED_BRANCHES if b in present]
    if default and default not in shared:
        shared.insert(0, default)
    if not shared:
        shared = [default or "main"]
    # The default first: it is the one a reader checks against their own repo.
    if default in shared:
        shared.remove(default)
        shared.insert(0, default)
    return default or shared[0], shared


def walk(root):
    """Every file under root, skipping the directories that are never source."""
    for path in root.rglob("*"):
        if path.is_dir() or any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def find_specs(root):
    """Candidate OpenAPI documents, richest first, and the ones that were
    rejected with the reason why.

    Richest rather than first-found: a repository often carries a stub spec next
    to the real one, and the one describing more operations is the one anybody
    would have meant.

    The rejections are returned rather than dropped because they used to be
    dropped, and the result was the worst error this tool produced: a repository
    whose spec was found and could not be read reported "no OpenAPI document
    found", sending the reader to look for a file that was sitting right there.
    A document that cannot be parsed is a different problem from one that does
    not exist, and only one of them is the reader's to fix.
    """
    seen, out, rejected = set(), [], []

    def consider(path):
        if path in seen:
            return
        seen.add(path)
        try:
            spec = load_spec(path)
        except Exception as exc:
            rejected.append((path, str(exc).replace(str(root) + "/", "")))
            return
        operations = len(spec.keys())
        if operations:
            out.append((operations, path, spec))
        else:
            rejected.append((path, "parsed, but documents no operations"))

    for directory in SPEC_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for name in SPEC_NAMES:
            path = base / name
            if path.is_file():
                consider(path)
    # Anything not in a conventional directory, so a spec at mintlify/v2/ or
    # docs/reference/ is still found rather than silently skipped.
    if not out:
        for path in walk(root):
            if path.name in SPEC_NAMES:
                consider(path)
    return sorted(out, key=lambda row: -row[0]), rejected


def server_prefix(spec):
    """The path component of the spec's first server URL, e.g. /api/v1.

    This is the prefix the code registers and the spec's paths leave out, which
    is what strip-prefix exists to reconcile.

    Swagger 2.0 spells it `basePath` at the top level; OpenAPI 3 replaced that
    with `servers`. Reading only the newer one left the prefix empty for every
    2.0 document, and an empty prefix means not one path matches: two of the
    twelve repositories this was measured on reported their whole API as
    undocumented and their whole document as unimplemented, from that alone.
    Swagger 2.0 is what `swaggo` still emits, which is most annotated Go.
    """
    for server in spec.document.get("servers", []) or []:
        url = str(server.get("url", ""))
        path = re.sub(r"^https?://[^/]+", "", url).rstrip("/")
        if path and path != "/":
            return path

    base = str(spec.document.get("basePath", "")).rstrip("/")
    if base and base != "/":
        return base if base.startswith("/") else "/" + base
    return ""


def route_table(adapter, directory, prefix):
    try:
        table = adapter.extract(directory, strip_prefix=prefix)
    except Exception:
        return None
    return table if (table or {}).get("routes") else None


def arbitrate(root, tied, prefix, spec_keys):
    """Which of the equally-plausible languages this specification belongs to.

    Marker files answer "what is written here", and on a polyglot repository the
    answer is legitimately several. They cannot answer "which of these serves
    the document being audited", and that is the question. So where the evidence
    ties, each candidate reads the repository and the one whose routes account
    for the most of the specification wins.

    Extraction is the expensive step, which is why this runs only on a tie and
    only over the candidates that tied.
    """
    if len(tied) == 1:
        return tied[0], (f"{tied[0].NAME} is the only language with a project "
                         f"marker at this level.")

    scored = []
    for adapter in tied:
        table = route_table(adapter, root, prefix)
        routes = (table or {}).get("routes") or []
        covered = len({(r["path"], r["method"].lower()) for r in routes} & spec_keys)
        scored.append((covered, len(routes), adapter))

    covered, _, adapter = max(scored)
    others = ", ".join(f"{a.NAME} {c}" for c, _, a in scored if a is not adapter)
    if covered:
        return adapter, (f"{covered} of the spec's {len(spec_keys)} documented "
                         f"operation(s) are registered in {adapter.NAME}, against "
                         f"{others}. Marker files alone tied "
                         f"{', '.join(a.NAME for a in tied)}.")
    # Nothing matched, so this is no better than the marker ordering. Say so
    # rather than dressing the fallback up as a derivation.
    return tied[0], (f"marker files tie {', '.join(a.NAME for a in tied)} and no "
                     f"candidate's routes matched a documented operation, so this "
                     f"is the most decisive marker rather than a measurement. "
                     f"Check it, and check strip-prefix.")


def descend(root, max_depth):
    """Directories under root, pruning the ones that never hold source.

    An explicit descent rather than rglob, because rglob walks .git and
    node_modules in full before any filter sees them, and on a large repository
    that is nearly all of the time spent.
    """
    stack = [(root, 0)]
    while stack:
        base, depth = stack.pop()
        yield base
        if depth >= max_depth:
            continue
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if (entry.is_dir() and not entry.is_symlink()
                    and entry.name not in SKIP_DIRS
                    and not entry.name.startswith(".")):
                stack.append((entry, depth + 1))


def service_roots(root, adapter):
    """Directories this repository declares a buildable unit, by manifest."""
    names = SERVICE_MANIFESTS.get(adapter.NAME, ())
    out = set()
    for directory in descend(root, MANIFEST_DEPTH):
        if any((directory / name).is_file() for name in names):
            relative = directory.relative_to(root).as_posix()
            out.add(relative or ".")
    return out


def routes_under(table, candidate):
    """The routes in a whole-repository table that belong to one directory."""
    if candidate == ".":
        return table["routes"]
    prefix = candidate.rstrip("/") + "/"
    return [r for r in table["routes"] if (r.get("file") or "").startswith(prefix)]


def pick_source(root, adapter, prefix, spec_keys=frozenset()):
    """The directory whose routes best account for the specification.

    Not the directory with the most routes, which is what this used to choose.
    On a repository holding more than one service the root always has the most,
    because it is the union of every one of them, and auditing that union
    reports every other service's routes as undocumented. On the first real
    repository this was pointed at that was 277 findings, 46 of them from two
    programs that do not serve this specification at all and never could.

    The specification is the thing being audited, so it is the thing that
    decides: of the directories this repository declares a unit, the one
    implementing the most of it, and among those the one carrying the fewest
    routes it never mentions.

    A candidate is only ever a conventional source directory or a directory with
    its own dependency manifest. Narrowing further - to whichever subdirectory
    happens to score best - would let the audit shrink to the routes that are
    already documented and report a clean bill of health, which is the one
    answer this tool must never give.
    """
    conventional = SOURCE_CANDIDATES.get(adapter.NAME, (".",))
    candidates = list(dict.fromkeys(
        [c for c in conventional if (root / c).is_dir()]
        + sorted(service_roots(root, adapter), key=lambda c: (c.count("/"), c))
        + ["."]))

    whole = route_table(adapter, root, prefix)

    scored = []
    for candidate in candidates:
        if not (root / candidate).is_dir():
            continue
        # The whole-repository table already says which routes live where, so
        # scoring costs nothing. Only the winner is extracted again, and only
        # because a nested directory resolves its own mount graph differently.
        routes = routes_under(whole, candidate) if whole else None
        if routes is None:
            table = route_table(adapter, (root / candidate).resolve(), prefix)
            if not table:
                continue
            routes = table["routes"]
        if not routes:
            continue
        covered = len({(r["path"], r["method"].lower()) for r in routes} & spec_keys)
        scored.append((covered, -(len(routes) - covered), candidate, len(routes)))

    if not scored:
        return None

    if spec_keys and max(row[0] for row in scored):
        covered, _, candidate, count = max(scored)
        why = (f"{covered} of the spec's {len(spec_keys)} documented operation(s) "
               f"are registered here, with fewer undocumented routes alongside "
               f"them than any other candidate directory.")
    else:
        # Nothing matched anything, so the spec cannot arbitrate. Either the
        # prefix is wrong or this really is a wholly undocumented API, and both
        # want the widest view rather than the narrowest.
        count, candidate = max((row[3], row[2]) for row in scored)
        why = (f"{count} route(s) extracted here, more than any other candidate "
               f"directory. No candidate matched any documented operation, so "
               f"the spec could not narrow this further - check strip-prefix "
               f"if the first run reports every route as undocumented.")

    table = (whole if candidate == "."
             else route_table(adapter, (root / candidate).resolve(), prefix))
    if not table:
        return None
    return len(table["routes"]), candidate, table, why


def contract_guards(root, table, headers, spec_keys=frozenset()):
    """Middleware whose source reads a credential the spec promises integrators,
    and which guards the endpoints that spec documents.

    The spec is the authority on what the contract's credential is: an apiKey
    security scheme names the header, and http auth means Authorization. A file
    that reads that header and exports a middleware the routes use is the guard
    that separates the promised API from everything else in the same codebase.

    Reading the header is necessary and not sufficient. Where the only declared
    scheme is http bearer - which is most specs - the credential is Authorization,
    and a session guard reads Authorization too. Both then qualify, and naming
    both as the contract puts the whole dashboard back inside it.

    So the spec arbitrates twice. It says what the credential is, and then it
    says which guard is protecting the endpoints it documents: the contract
    guard is the one whose routes account for most of the specification. A
    session guard scores near zero against a spec it does not serve, however
    many Authorization headers it reads.
    """
    used = sorted({m for route in table["routes"] for m in (route.get("middleware") or [])})
    if not used or not headers:
        return [], used, {}

    # Authorization is worth almost nothing on its own: a session guard reads it
    # too. It only discriminates when the spec offers no better credential.
    strong = {h for h in headers if h.lower() != "authorization"}
    reading = sorted(guards_reading(root, used, strong or headers))
    if not reading or not spec_keys:
        return reading, used, {}

    documented = {
        name: len({(r["path"], r["method"].lower())
                   for r in table["routes"]
                   if name in (r.get("middleware") or [])} & spec_keys)
        for name in reading
    }
    # Half is the line because a guard covering less than half of what the spec
    # documents cannot be the thing defining it, while one covering more is not
    # excluded by a second guard layered on top of it.
    # Half is the line in both directions. A guard covering more than half of
    # what the spec documents is defining it, even with a second guard layered
    # on top. A guard covering less is not, however many Authorization headers
    # it reads - and naming it would filter the audit down to the wrong API,
    # which is worse than not filtering at all. So an empty answer is a real
    # answer here, and the caller says why.
    covering = sorted(n for n, count in documented.items()
                      if count * 2 >= len(spec_keys))
    return covering, used, documented


def render(findings):
    """The workflow file.

    Complete on purpose: every input this action takes is present, with the
    optional ones pointing at secrets that may not exist yet. Each is skipped
    silently when its secret is unset, so the file works exactly as generated
    and turning a feature on never means editing YAML - only adding a secret.
    Nobody should have to come back here and add a section; the only edits this
    file should ever need are deletions.
    """
    f = findings
    branches = ", ".join(f["branches"])
    lines = [
        "name: Contract Audit",
        "",
        "# Generated by contract-auditor init. Every value was derived from this",
        "# repository and the reason is beside it. Read them once: a wrong spec or",
        "# source-dir produces a report about the wrong thing rather than an error.",
        "#",
        "# Everything optional is already here, pointing at secrets that may not",
        "# exist. Each is skipped silently when its secret is unset, so this runs",
        "# as it stands. Add a secret to turn one on. Delete what you will never",
        "# use. You should not need to add anything.",
        "",
        "on:",
    ]
    lines += comment(f["branches_why"] + " Deleting a branches line audits every "
                                         "branch instead.", indent="  ")
    lines += [
        "  pull_request:",
        f"    branches: [{branches}]",
        "  push:",
        "    # The merged result, which no pull request run sees: one pull request",
        "    # can add a route while another edits the spec, and each is audited",
        "    # against its own merge preview rather than what they both land on.",
        f"    branches: [{branches}]",
    ]
    lines += [
        "  schedule:",
        "    # contract-auditor@v1 is a floating tag, so new rules can find drift in",
        "    # code that has not changed. Delete this if that is not worth a run.",
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
        "        # The audit never fails this job while fail-on is none. Tighten to",
        "        # critical, then high, once the first backlog is cleared.",
        "        continue-on-error: true",
        "        with:",
        *comment(f["spec_why"]),
        f"          spec: {f['spec']}",
        *comment(f["source_why"]),
        f"          source-dir: {f['source_dir']}",
        *comment(f["language_why"]),
        f"          language: {f['language']}",
    ]

    if f["strip_prefix"]:
        lines += comment(f["prefix_why"]) + [
            f"          strip-prefix: {f['strip_prefix']}"]
    else:
        lines += comment(
            "no servers[].url path in the spec, so there is nothing to strip. If "
            "the first run reports every route as missing from the spec, this is "
            "the input that is wrong.") + ['          strip-prefix: ""']

    lines.append("")
    if f["guards"]:
        lines += comment(f["guard_why"]) + [
            "          contract-middleware: " + ", ".join(f["guards"])]
    else:
        lines += comment("contract-middleware not set: " + f["guard_why"]) + [
            '          contract-middleware: ""']

    lines.append("")
    lines += comment(
        "Paths to drop from the audit entirely, on both sides, for routes "
        "contract-middleware does not already separate. Matched after "
        "strip-prefix is removed; a trailing /* covers the collection itself.")
    lines += [
        "          # exclude-paths: |",
        "          #   /internal/*",
        "          #   /admin/*",
        "",
        "          # Report only. critical, high, medium, low, or none.",
        "          fail-on: none",
        "",
    ]
    lines += comment(
        "The judgment pass. Three kinds of drift need reading comprehension "
        "rather than lookup: a field the code secretly insists on, a rule quietly "
        "relaxed, a default changed. Without a key only the deterministic layer "
        "runs, which costs nothing and still catches most drift.")
    lines += [
        "          api-key: ${{ secrets.OPENROUTER_API_KEY }}",
        "          workers: '8'",
        "          # model: z-ai/glm-5.3-flash",
        "          # reasoning: medium",
        "",
    ]
    lines += comment(
        "Notifications. Counts and severities only, never endpoint paths: a "
        "channel holds people who cannot read the code.")
    lines += [
        "          slack-webhook-url: ${{ secrets.CONTRACT_AUDIT_SLACK_WEBHOOK_URL }}",
        "          telegram-bot-token: ${{ secrets.CONTRACT_AUDIT_TELEGRAM_BOT_TOKEN }}",
        "          telegram-chat-id: ${{ secrets.CONTRACT_AUDIT_TELEGRAM_CHAT_ID }}",
        "          notify-min-severity: critical",
        "",
    ]
    lines += comment("The full JSON report POSTed to a sink of your own, signed "
                     "with the secret as X-Auditor-Token.")
    lines += [
        "          webhook-url: ${{ secrets.CONTRACT_AUDIT_WEBHOOK_URL }}",
        "          webhook-secret: ${{ secrets.CONTRACT_AUDIT_WEBHOOK_SECRET }}",
        "",
    ]
    lines += comment(
        "Self-improvement. Verdicts kept in storage you own, so later runs know "
        "which kinds of complaint have been refuted before. Nothing is stored in "
        "this repository or inside the image, and no memory is shared between "
        "projects. Any S3-compatible bucket, an HTTPS endpoint, Cloudinary or IPFS.")
    lines += [
        "          memory-url: ${{ secrets.CONTRACT_AUDIT_MEMORY_URL }}",
        "          memory-key-id: ${{ secrets.CONTRACT_AUDIT_MEMORY_KEY_ID }}",
        "          memory-secret: ${{ secrets.CONTRACT_AUDIT_MEMORY_SECRET }}",
        "          memory-token: ${{ secrets.CONTRACT_AUDIT_MEMORY_TOKEN }}",
        "          memory-region: ${{ secrets.CONTRACT_AUDIT_MEMORY_REGION }}",
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
        '          cp "$SUMMARY" contract-audit-comment.md',
        "          # The link is for a person with a browser session behind it.",
        "          # An agent has neither, and the artifact URL is not fetchable",
        "          # with curl, so the block below is what gets handed to one:",
        "          # GitHub puts a copy button on a fenced block, and gh unzips",
        "          # as it downloads, so there is no zip to extract by hand.",
        '          if [ -n "$BRIEF_URL" ]; then',
        "            {",
        "              printf '\\n**[Download the fix brief](%s)** - a zip of "
        "the brief and its tests.\\n' \"$BRIEF_URL\"",
        "              printf '\\nOr copy this and hand it to a coding agent, "
        "which downloads and unzips it:\\n\\n'",
        "              printf '```bash\\n'",
        "              printf 'gh run download %s -R %s -n contract-audit-brief "
        "-D contract-audit-brief\\n' \\",
        '                "$GITHUB_RUN_ID" "$GITHUB_REPOSITORY"',
        "              printf 'cat contract-audit-brief/*_brief.md   # then "
        "apply what it lists\\n'",
        "              printf '```\\n'",
        "            } >> contract-audit-comment.md",
        "          fi",
        '          echo "path=contract-audit-comment.md" >> "$GITHUB_OUTPUT"',
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


def inspect(root, branches=()):
    root = pathlib.Path(root).resolve()

    if branches:
        default, branches = branches[0], list(branches)
        branches_why = "Branches given on the command line."
    else:
        default, branches = git_branches(root)
        reached = bool(remote_branches(root))
        branches_why = (
            f"{default} is this repository's default branch. These are the "
            f"shared branches "
            + ("the remote has" if reached else
               "this clone knows about; the remote was not reachable, so a "
               "branch it has and this clone never fetched is missing")
            + ". Add or remove to match how your team merges.")

    tied = languages.tied(root)
    if not tied:
        raise SystemExit(
            f"could not identify a language in {root}. Supported: "
            f"{', '.join(languages.names())}. Point --repo at the directory "
            f"holding go.mod, package.json, pyproject.toml or artisan.")
    adapter = tied[0]

    specs, rejected = find_specs(root)
    if not specs:
        if rejected:
            listed = "\n  ".join(
                f"{p.relative_to(root).as_posix()}: {why}" for p, why in rejected)
            raise SystemExit(
                f"found {len(rejected)} OpenAPI document(s) under {root} and could "
                f"read none of them:\n  {listed}\n"
                f"Fix the document, or point --spec at one that parses.")
        raise SystemExit(
            f"no OpenAPI document found under {root}. Looked for "
            f"{', '.join(SPEC_NAMES)} in {', '.join(SPEC_DIRS)}. There is nothing "
            f"to audit code against until one exists.")
    operations, spec_path, spec = specs[0]

    prefix = server_prefix(spec)
    spec_keys = set(spec.keys())

    # A polyglot repository can look equally like two languages, and the marker
    # ordering then decides by convention rather than by evidence. The spec is
    # better evidence than any convention: the language that implements the
    # document is the language the document is about.
    adapter, language_why = arbitrate(root, tied, prefix, spec_keys)

    picked = pick_source(root, adapter, prefix, spec_keys)
    if picked is None:
        raise SystemExit(
            f"no routes extracted from any of "
            f"{', '.join(SOURCE_CANDIDATES.get(adapter.NAME, ('.',)))} in {root}. "
            f"The language was detected as {adapter.NAME}; if that is wrong, the "
            f"marker file that decided it is not this project's main language.")
    route_count, source_dir, table, source_why = picked

    headers = auth_header_names(spec)
    guards, used, considered = contract_guards(root, table, headers, spec_keys)

    if guards:
        guard_why = (f"{', '.join(sorted(headers))} is the credential this spec "
                     f"promises integrators, and these read it. Routes behind any "
                     f"other guard are a different API and leave the audit.")
    elif not used:
        guard_why = (f"the {adapter.NAME} extractor records no route middleware, "
                     f"so the contract cannot be told from a console by its guard. "
                     f"Use exclude-paths below instead.")
    elif not headers:
        guard_why = ("this spec declares no securitySchemes, so nothing names the "
                     "credential integrators were promised. Routes are guarded by "
                     f"{', '.join(used[:4])}; name the contract guard by hand, or use "
                     f"exclude-paths below.")
    elif considered:
        # They read the credential but none of them protects the API the spec
        # describes, so naming one would filter the audit down to the wrong
        # thing. Saying which were weighed, and by how much they missed, is what
        # lets a reader overrule this in one line.
        ranked = ", ".join(f"{name} guards {count} of them"
                           for name, count in sorted(considered.items(),
                                                     key=lambda kv: -kv[1])[:4])
        guard_why = (f"the spec documents {len(spec_keys)} operation(s) and no "
                     f"single guard protects even half: {ranked}. Naming one would "
                     f"audit the wrong API, so nothing is set. Name the contract "
                     f"guard by hand, or use exclude-paths below.")
    else:
        guard_why = (f"none of {', '.join(used[:4])} reads "
                     f"{', '.join(sorted(headers))}. Name the contract guard by "
                     f"hand, or use exclude-paths below.")

    return {
        "language": adapter.NAME,
        "language_why": language_why,
        "spec": spec_path.relative_to(root).as_posix(),
        "spec_why": f"{operations} documented operation(s), the richest spec found.",
        "source_dir": source_dir,
        "source_why": source_why,
        "strip_prefix": prefix,
        "prefix_why": "the path component of this spec's own servers[].url.",
        "guards": guards,
        "guard_why": guard_why,
        "middleware_seen": used,
        "routes": route_count,
        "operations": operations,
        "branches": branches,
        "branches_why": branches_why,
    }


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".", help="the repository to read")
    parser.add_argument("--branch", action="append", default=[],
                        help="branch to audit; repeatable. Detected from the "
                             "remote when not given")
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

    findings = inspect(args.repo, tuple(args.branch))
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
            # A diff, not an instruction to go and read two files. The question
            # anyone has here is what would change, and printing the whole
            # generated workflow to answer it makes them do the comparing.
            current = path.read_text()
            if current == workflow:
                print(f"{path} is already exactly what init would write.",
                      file=sys.stderr)
                return
            sys.stdout.writelines(difflib.unified_diff(
                current.splitlines(keepends=True),
                workflow.splitlines(keepends=True),
                fromfile=f"{args.out} (yours)", tofile=f"{args.out} (init)", n=2))
            # stderr is unbuffered and stdout is not, so without this the
            # summary prints above the diff it is summarising.
            sys.stdout.flush()
            print(f"\n{path} was left alone. --force replaces it, --out PATH "
                  f"writes elsewhere, --stdout prints without comparing.",
                  file=sys.stderr)
            raise SystemExit(1)
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
