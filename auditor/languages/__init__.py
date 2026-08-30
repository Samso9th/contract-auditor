"""Language adapters.

Most of this auditor never knew what language it was reading. The spec index,
the drift rules, the per-endpoint agent, the scorer and the reporters all work on
two neutral structures: a route table and an OpenAPI document. Only two things
were ever Go-specific: extracting the route table from source, and generating a
test that executes one endpoint.

An adapter supplies exactly those two things:

    name                  identifier used in output and CLI flags
    detect(directory)     is this a project of that kind?
    default_prefix        path prefix usually stripped, e.g. "/api/v1"
    extract(dir, prefix)  the route table, in the shape tools/routes.py returns
    render_test(...)      source for a test that probes one endpoint
    test_command(dir)     argv that runs it
    test_path(dir, route) where to write it

A language counts as supported only when it has its own fixture, its own
injected mutations, and a passing evaluation. Shipping a parser and calling that
support would be claiming a contract we had not verified, the exact failure this
tool exists to catch.
"""

from __future__ import annotations

import pathlib

from . import go_lang, php_lang, python_lang, typescript

ADAPTERS = {
    go_lang.NAME: go_lang,
    typescript.NAME: typescript,
    python_lang.NAME: python_lang,
    php_lang.NAME: php_lang,
}

# Ordered by how decisive the marker is, so a polyglot repository resolves to
# the language whose marker is least likely to be incidental.
DETECTION_ORDER = (go_lang, php_lang, typescript, python_lang)


# Directories that hold other people's code. A single .go file shipped inside an
# npm package - node_modules/flatted/golang/pkg/flatted/flatted.go is a real one,
# and flatted is a dependency of eslint - is enough to make a TypeScript project
# detect as Go when the scan does not skip these.
VENDORED = {".git", "node_modules", "vendor", "dist", "build", "__pycache__",
            ".venv", "venv", ".next", "target", "coverage", ".mypy_cache",
            "site-packages", ".tox", "bower_components"}


def has_source(directory, suffix):
    """Whether the project itself contains a file of that kind.

    rglob alone answers a different question: whether anything anywhere under
    here does, including every dependency that was ever installed.
    """
    for path in pathlib.Path(directory).rglob(f"*{suffix}"):
        if not any(part in VENDORED for part in path.parts):
            return True
    return False


# What each toolchain requires at the root of anything it can build or install,
# and the files each language is written in.
MANIFESTS = {
    "go": ("go.mod",),
    "typescript": ("package.json",),
    "python": ("pyproject.toml", "setup.py", "Pipfile", "requirements.txt"),
    "php": ("composer.json", "artisan"),
}
SUFFIXES = {
    "go": (".go",),
    "typescript": (".ts", ".tsx", ".js", ".mjs"),
    "python": (".py",),
    "php": (".php",),
}

# Deep enough to find apps/api and services/gateway, shallow enough to stay fast.
MANIFEST_DEPTH = 4

ROOT_MANIFEST, NESTED_MANIFEST, SOURCE_ONLY = 3, 2, 1


def _directories(root, max_depth):
    """Directories under root, pruning what is never a project's own source.

    An explicit descent rather than rglob: rglob walks node_modules and .git in
    full before any filter sees them, which on a large repository is nearly all
    of the time spent.
    """
    stack = [(pathlib.Path(root), 0)]
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
                    and entry.name not in VENDORED
                    and not entry.name.startswith(".")):
                stack.append((entry, depth + 1))


def evidence(directory, adapter):
    """How strongly this repository looks like a project of that language.

        3  its manifest is at the root
        2  its manifest is somewhere below the root
        1  only source files of that kind, no manifest
        0  nothing

    A stray file is not a language, and first-match-wins on a fixed order
    treated it as one. firecrawl is a TypeScript API carrying one small Go
    service beside it, and honcho is a Python service with a TypeScript package
    inside it; both were identified by the thing they are not, and neither
    project's actual API was ever read.

    Weighing the evidence rather than ordering it is what separates the two. A
    manifest is not a convention someone chose - the toolchain will not build
    without it - so its presence, and where, is the strongest thing a directory
    can say about what it is.
    """
    directory = pathlib.Path(directory)
    manifests = MANIFESTS.get(adapter.NAME, ())
    if any((directory / name).is_file() for name in manifests):
        return ROOT_MANIFEST
    for base in _directories(directory, MANIFEST_DEPTH):
        if any((base / name).is_file() for name in manifests):
            return NESTED_MANIFEST
    if any(has_source(directory, suffix) for suffix in SUFFIXES.get(adapter.NAME, ())):
        return SOURCE_ONLY
    return 0


def candidates(directory):
    """Every language this could be, strongest evidence first.

    Ties keep DETECTION_ORDER, which orders by how decisive a marker is. A
    caller holding the specification should break the tie with it instead: see
    init.py, where the language whose routes account for the document wins.
    """
    directory = pathlib.Path(directory)
    scored = []
    for order, adapter in enumerate(DETECTION_ORDER):
        score = evidence(directory, adapter)
        if score:
            scored.append((-score, order, adapter))
    return [adapter for _, _, adapter in sorted(scored)]


def tied(directory):
    """The candidates sharing the strongest evidence, which is what a spec is
    worth consulting about."""
    directory = pathlib.Path(directory)
    ranked = candidates(directory)
    if not ranked:
        return []
    best = evidence(directory, ranked[0])
    return [a for a in ranked if evidence(directory, a) == best]


def detect(directory):
    """Identify the project's language, or None when nothing matches."""
    ranked = candidates(directory)
    return ranked[0] if ranked else None


def get(name):
    if name in ADAPTERS:
        return ADAPTERS[name]
    raise KeyError(f"unknown language {name!r}; known: {', '.join(sorted(ADAPTERS))}")


def names():
    return sorted(ADAPTERS)
