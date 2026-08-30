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


def detect(directory):
    """Identify the project's language, or None when nothing matches."""
    directory = pathlib.Path(directory)
    for adapter in DETECTION_ORDER:
        if adapter.detect(directory):
            return adapter
    return None


def get(name):
    if name in ADAPTERS:
        return ADAPTERS[name]
    raise KeyError(f"unknown language {name!r}; known: {', '.join(sorted(ADAPTERS))}")


def names():
    return sorted(ADAPTERS)
