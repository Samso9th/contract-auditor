#!/usr/bin/env bash
# Entrypoint for the GitHub Action. Reads its configuration from the INPUT_*
# variables the Actions runner sets, audits the checked-out repository, and
# emits results on every channel that is configured.
set -euo pipefail

AUDITOR=/opt/auditor

SOURCE_DIR="${INPUT_SOURCE_DIR:-.}"
SPEC="${INPUT_SPEC:-}"
LANGUAGE="${INPUT_LANGUAGE:-auto}"
STRIP_PREFIX="${INPUT_STRIP_PREFIX:-}"
MODEL="${INPUT_MODEL:-z-ai/glm-5.3-flash}"
WORKERS="${INPUT_WORKERS:-8}"
FAIL_ON="${INPUT_FAIL_ON:-high}"
SARIF="${INPUT_SARIF_PATH:-contract-audit.sarif}"
SUMMARY="${INPUT_SUMMARY_PATH:-contract-audit.md}"
OUT_DIR="${RUNNER_TEMP:-/tmp}/contract-audit"

export OPENROUTER_API_KEY="${INPUT_API_KEY:-${OPENROUTER_API_KEY:-}}"
export OPENROUTER_BASE_URL="${INPUT_BASE_URL:-${OPENROUTER_BASE_URL:-https://openrouter.ai/api/v1}}"
export AUDITOR_WEBHOOK_URL="${INPUT_WEBHOOK_URL:-}"
export AUDITOR_WEBHOOK_SECRET="${INPUT_WEBHOOK_SECRET:-}"
export SLACK_WEBHOOK_URL="${INPUT_SLACK_WEBHOOK_URL:-${SLACK_WEBHOOK_URL:-}}"
export TELEGRAM_BOT_TOKEN="${INPUT_TELEGRAM_BOT_TOKEN:-${TELEGRAM_BOT_TOKEN:-}}"
export TELEGRAM_CHAT_ID="${INPUT_TELEGRAM_CHAT_ID:-${TELEGRAM_CHAT_ID:-}}"
NOTIFY_MIN="${INPUT_NOTIFY_MIN_SEVERITY:-high}"

log() { printf '%s\n' "$*"; }
fail() { printf '::error::%s\n' "$*" >&2; exit 1; }

[ -n "$SPEC" ] || fail "spec input is required — the path to your OpenAPI document"
[ -f "$SPEC" ] || fail "spec not found at '$SPEC'"
[ -d "$SOURCE_DIR" ] || fail "source-dir not found at '$SOURCE_DIR'"

mkdir -p "$OUT_DIR"

LANG_ARG=()
[ "$LANGUAGE" != "auto" ] && LANG_ARG=(--language "$LANGUAGE")

log "::group::Contract audit"
log "source     $SOURCE_DIR"
log "spec       $SPEC"
log "language   $LANGUAGE"
log "model      $MODEL"

# No key means the deterministic layer only. That is a genuinely useful mode —
# it catches most drift, costs nothing, and needs no secret — so it runs rather
# than failing the job.
if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  log "no api-key supplied — running the deterministic layer only (no model calls)"
  python3 "$AUDITOR/auditor/tools/diff.py" "$SOURCE_DIR" "$SPEC" \
      ${STRIP_PREFIX:+--strip-prefix "$STRIP_PREFIX"} "${LANG_ARG[@]}" \
      --json > "$OUT_DIR/report.json"
else
  python3 "$AUDITOR/auditor/run.py" --repo "$SOURCE_DIR" --spec "$SPEC" \
      --model "$MODEL" --workers "$WORKERS" \
      ${STRIP_PREFIX:+--strip-prefix "$STRIP_PREFIX"} "${LANG_ARG[@]}" \
      --out "$OUT_DIR"
fi
log "::endgroup::"

REPORT="$OUT_DIR/report.json"
[ -f "$REPORT" ] || fail "the audit produced no report"

set +e
python3 "$AUDITOR/auditor/report.py" "$REPORT" \
    --sarif "$SARIF" --markdown "$SUMMARY" \
    --repo-subdir "$([ "$SOURCE_DIR" = "." ] && echo "" || echo "$SOURCE_DIR")" \
    --fail-on "$FAIL_ON" --title "Contract audit"
STATUS=$?
set -e

# Human channels are a separate concern from CI artifacts: notify.py sends only
# verified findings and stays silent on a clean run, because an alert that fires
# on every build stops being read within a week.
if [ -n "${SLACK_WEBHOOK_URL:-}" ] || [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  RUN_URL="${GITHUB_SERVER_URL:-}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}"
  python3 "$AUDITOR/auditor/notify.py" --run "$REPORT" \
      --title "Contract audit — ${GITHUB_REPOSITORY:-repository}" \
      --min-severity "$NOTIFY_MIN" \
      ${GITHUB_RUN_ID:+--report-url "$RUN_URL"} || \
    log "::warning::notification delivery failed; the audit itself succeeded"
fi

TOTAL=$(jq '.findings | length' "$REPORT" 2>/dev/null || echo 0)
CRITICAL=$(jq '[.findings[] | select(.severity=="critical")] | length' "$REPORT" 2>/dev/null || echo 0)
HIGH=$(jq '[.findings[] | select(.severity=="high")] | length' "$REPORT" 2>/dev/null || echo 0)

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    echo "findings=$TOTAL"
    echo "critical=$CRITICAL"
    echo "high=$HIGH"
    echo "sarif=$SARIF"
    echo "summary=$SUMMARY"
  } >> "$GITHUB_OUTPUT"
fi

# The job summary renders on the run page with no extra configuration, so there
# is something to read even before SARIF upload or PR comments are wired up.
if [ -n "${GITHUB_STEP_SUMMARY:-}" ] && [ -f "$SUMMARY" ]; then
  cat "$SUMMARY" >> "$GITHUB_STEP_SUMMARY"
fi

log "findings: $TOTAL (critical $CRITICAL, high $HIGH)"
exit $STATUS
