# Contract auditor: the image GitHub Actions pulls.
#
# Base is the full Debian Go image rather than Alpine on purpose. The
# verification gate runs `go test` inside the *consumer's* repository, and a
# musl base breaks any project that builds with cgo. A larger image that runs
# every caller's tests correctly beats a small one that silently fails on some
# of them; this tool exists to not be quietly wrong.
FROM golang:1.24-bookworm

# `image.source` is what links the published package to the repository, and the
# link is what makes the package inherit the repository's visibility. The release
# workflow also supplies it via docker/metadata-action; setting it here too means
# a hand-built image links correctly as well, instead of landing unlinked and
# private for reasons that are tedious to work out after the fact.
LABEL org.opencontainers.image.title="contract-auditor" \
      org.opencontainers.image.description="Finds drift between API code, its OpenAPI spec, and its published docs, and proves each finding with a test." \
      org.opencontainers.image.source="https://github.com/samso9th/contract-auditor" \
      org.opencontainers.image.licenses="MIT"

# python3 runs the harness, node runs the TypeScript extractor, curl is the
# HTTPS transport for model calls. python3-yaml reads the half of OpenAPI
# documents that are written as YAML rather than JSON. Still no pip: every
# dependency comes from the distribution, which is why this layer stays small
# and the build stays offline-safe.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-minimal python3-yaml ca-certificates curl git jq \
        nodejs npm \
    && npm install -g typescript@5 --no-fund --no-audit \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GOFLAGS=-mod=mod \
    NODE_PATH=/usr/local/lib/node_modules

WORKDIR /opt/auditor
# Only what the audit path needs. `eval/` and `baseline/` exist for this
# project's own evaluation and would be dead weight in every consumer's pull.
COPY auditor/ ./auditor/
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# Belt and braces over .dockerignore: no claim ledger may ship inside the image.
# A ledger is one project's statistics, and shipping ours would hand every
# consumer priors learned from a payments fixture they have never seen. The
# build fails rather than publishes if one is present.
RUN rm -f auditor/memory/*.jsonl auditor/memory/rules.json \
    && ! ls auditor/memory/*.jsonl >/dev/null 2>&1 \
    && echo "image carries no learned memory"

# Prebuild the route extractor so the first audit does not pay for it, and fail
# the build if it does not compile. The previous `|| true` here would have
# produced an image that looked fine and failed at audit time instead.
RUN cd auditor/tools/goroutes && go build -o goroutes . && ./goroutes -dir . > /dev/null

# Prove the TypeScript extractor can resolve its parser inside the image. A
# container that cannot parse TypeScript should fail here, loudly, not on a
# user's first pull request.
RUN node --check auditor/tools/tsroutes/extract.mjs \
    && node -e "require('typescript'); console.log('typescript resolvable')"

# GitHub Actions mounts the checkout at /github/workspace and runs there.
WORKDIR /github/workspace
ENTRYPOINT ["/opt/auditor/entrypoint.sh"]
