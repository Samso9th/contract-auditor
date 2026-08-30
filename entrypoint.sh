#!/usr/bin/env bash
# Entrypoint for the GitHub Action. Reads its configuration from the INPUT_*
# variables the Actions runner sets, audits the checked-out repository, and
# emits results on every channel that is configured.
set -euo pipefail

AUDITOR=/opt/auditor

# The runner uppercases an input id and replaces spaces with underscores, but it
# leaves hyphens alone: `source-dir` arrives as INPUT_SOURCE-DIR, which is not a
# name any shell can expand. Read it out of the environment instead, falling back
# to the underscored spelling so the image stays runnable by hand with
# `docker run -e INPUT_SOURCE_DIR=...`.
input() {
  local value
  value="$(printenv "INPUT_$1" || true)"
  [ -n "$value" ] || value="$(printenv "INPUT_${1//-/_}" || true)"
  printf '%s' "$value"
}

SOURCE_DIR="$(input SOURCE-DIR)"; SOURCE_DIR="${SOURCE_DIR:-.}"
SPEC="$(input SPEC)"
LANGUAGE="$(input LANGUAGE)"; LANGUAGE="${LANGUAGE:-auto}"
STRIP_PREFIX="$(input STRIP-PREFIX)"
EXCLUDE_PATHS="$(input EXCLUDE-PATHS)"
MODEL="$(input MODEL)"; MODEL="${MODEL:-z-ai/glm-5.3-flash}"
# Empty means send no reasoning field at all and let the model do what it
# normally does, which is what the published numbers were measured under.
REASONING="$(input REASONING)"
WORKERS="$(input WORKERS)"; WORKERS="${WORKERS:-8}"
FAIL_ON="$(input FAIL-ON)"; FAIL_ON="${FAIL_ON:-high}"
SARIF="$(input SARIF-PATH)"; SARIF="${SARIF:-contract-audit.sarif}"
SUMMARY="$(input SUMMARY-PATH)"; SUMMARY="${SUMMARY:-contract-audit.md}"
OUT_DIR="${RUNNER_TEMP:-/tmp}/contract-audit"

# Each of these falls back to an ambient variable of the same name, so the image
# stays usable outside Actions. The right-hand side is evaluated before the
# assignment lands, so reading the variable being exported is safe.
IN_API_KEY="$(input API-KEY)"
export OPENROUTER_API_KEY="${IN_API_KEY:-${OPENROUTER_API_KEY:-}}"
IN_BASE_URL="$(input BASE-URL)"
export OPENROUTER_BASE_URL="${IN_BASE_URL:-${OPENROUTER_BASE_URL:-https://openrouter.ai/api/v1}}"
export AUDITOR_WEBHOOK_URL="$(input WEBHOOK-URL)"
export AUDITOR_WEBHOOK_SECRET="$(input WEBHOOK-SECRET)"
# Self-improvement is a layer, exactly like the model and the notifications: it
# exists only when the operator points it at storage they own. No url means no
# memory, and in that case nothing is read, nothing is written, and the audit is
# unchanged. Nothing is ever stored in the checkout or inside this image.
IN_MEMORY_URL="$(input MEMORY-URL)"
IN_MEMORY_KEY_ID="$(input MEMORY-KEY-ID)"
IN_MEMORY_SECRET="$(input MEMORY-SECRET)"
IN_MEMORY_TOKEN="$(input MEMORY-TOKEN)"
IN_MEMORY_REGION="$(input MEMORY-REGION)"
export AUDITOR_MEMORY_URL="${IN_MEMORY_URL:-${AUDITOR_MEMORY_URL:-}}"
export AUDITOR_MEMORY_KEY_ID="${IN_MEMORY_KEY_ID:-${AUDITOR_MEMORY_KEY_ID:-}}"
export AUDITOR_MEMORY_SECRET="${IN_MEMORY_SECRET:-${AUDITOR_MEMORY_SECRET:-}}"
export AUDITOR_MEMORY_TOKEN="${IN_MEMORY_TOKEN:-${AUDITOR_MEMORY_TOKEN:-}}"
export AUDITOR_MEMORY_REGION="${IN_MEMORY_REGION:-${AUDITOR_MEMORY_REGION:-}}"

IN_SLACK="$(input SLACK-WEBHOOK-URL)"
IN_TG_TOKEN="$(input TELEGRAM-BOT-TOKEN)"
IN_TG_CHAT="$(input TELEGRAM-CHAT-ID)"
export SLACK_WEBHOOK_URL="${IN_SLACK:-${SLACK_WEBHOOK_URL:-}}"
export TELEGRAM_BOT_TOKEN="${IN_TG_TOKEN:-${TELEGRAM_BOT_TOKEN:-}}"
export TELEGRAM_CHAT_ID="${IN_TG_CHAT:-${TELEGRAM_CHAT_ID:-}}"
NOTIFY_MIN="$(input NOTIFY-MIN-SEVERITY)"; NOTIFY_MIN="${NOTIFY_MIN:-high}"

log() { printf '%s\n' "$*"; }
fail() { printf '::error::%s\n' "$*" >&2; exit 1; }

[ -n "$SPEC" ] || fail "spec input is required: the path to your OpenAPI document"
[ -f "$SPEC" ] || fail "spec not found at '$SPEC'"
[ -d "$SOURCE_DIR" ] || fail "source-dir not found at '$SOURCE_DIR'"

mkdir -p "$OUT_DIR"

LANG_ARG=()
[ "$LANGUAGE" != "auto" ] && LANG_ARG=(--language "$LANGUAGE")

REASONING_ARG=()
[ -n "$REASONING" ] && REASONING_ARG=(--reasoning "$REASONING")

# An array rather than ${VAR:+...}: the value is multi-line by design, and word
# splitting would turn one pattern per line into one pattern per word.
EXCLUDE_ARG=()
[ -n "$EXCLUDE_PATHS" ] && EXCLUDE_ARG=(--exclude-paths "$EXCLUDE_PATHS")

log "::group::Contract audit"
log "source     $SOURCE_DIR"
log "spec       $SPEC"
log "language   $LANGUAGE"
[ -z "$EXCLUDE_PATHS" ] || log "excluding  $(printf '%s' "$EXCLUDE_PATHS" | tr '\n' ' ')"
log "model      $MODEL"
log "reasoning  ${REASONING:-provider default}"
if [ -n "${AUDITOR_MEMORY_URL:-}" ]; then
  # The scheme and host are safe to print and useful when a store misbehaves.
  # The path, the keys and the token are not printed anywhere, ever.
  log "memory     ${AUDITOR_MEMORY_URL%%/*}// (self-improvement on, gate enabled)"
else
  log "memory     off (no memory-url; set one to turn on self-improvement)"
fi

# No key means the deterministic layer only. That is a genuinely useful mode:
# it catches most drift, costs nothing, and needs no secret, so it runs rather
# than failing the job.
if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  log "no api-key supplied, running the deterministic layer only (no model calls)"
  python3 "$AUDITOR/auditor/tools/diff.py" "$SOURCE_DIR" "$SPEC" \
      ${STRIP_PREFIX:+--strip-prefix "$STRIP_PREFIX"} "${LANG_ARG[@]}" \
      "${EXCLUDE_ARG[@]}" --json > "$OUT_DIR/report.json"
else
  python3 "$AUDITOR/auditor/run.py" --repo "$SOURCE_DIR" --spec "$SPEC" \
      --model "$MODEL" --workers "$WORKERS" "${REASONING_ARG[@]}" \
      ${STRIP_PREFIX:+--strip-prefix "$STRIP_PREFIX"} "${LANG_ARG[@]}" \
      "${EXCLUDE_ARG[@]}" --out "$OUT_DIR"
fi
log "::endgroup::"

REPORT="$OUT_DIR/report.json"
[ -f "$REPORT" ] || fail "the audit produced no report"

# The fix brief: one per run, with every finding and the test that proved it.
# Written next to the workspace so the workflow can upload it as an artifact,
# which is why this needs no external storage of any kind.
BRIEF_NAME="$(input BRIEF-NAME)"; BRIEF_NAME="${BRIEF_NAME:-contract-audit}"
BRIEF_DIR="$(input BRIEF-DIR)"; BRIEF_DIR="${BRIEF_DIR:-contract-audit-brief}"
mkdir -p "$BRIEF_DIR"
# The archive goes beside the directory, not inside it. actions/upload-artifact
# zips whatever it is given, so a zip within the uploaded directory arrives as a
# zip inside a zip: two copies of the same brief, one of them buried.
BRIEF_MD="$BRIEF_DIR/${BRIEF_NAME}_brief.md"
BRIEF_ZIP="${BRIEF_DIR%/}.zip"
python3 "$AUDITOR/auditor/brief.py" "$REPORT" \
    --name "$BRIEF_NAME" --out "$BRIEF_DIR" --zip-path "$BRIEF_ZIP" \
    --repo "${GITHUB_REPOSITORY:-}" --sha "${GITHUB_SHA:-}" \
    --run-url "${GITHUB_SERVER_URL:-}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}" \
    || log "::warning::could not build the fix brief; the audit itself succeeded"

set +e
python3 "$AUDITOR/auditor/report.py" "$REPORT" \
    --sarif "$SARIF" --markdown "$SUMMARY" \
    ${BRIEF_MD:+--brief "$BRIEF_MD"} --artifact-name "$BRIEF_DIR" \
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
      --title "Contract audit: ${GITHUB_REPOSITORY:-repository}" \
      --min-severity "$NOTIFY_MIN" \
      ${GITHUB_RUN_ID:+--report-url "$RUN_URL"} || \
    log "::warning::notification delivery failed; the audit itself succeeded"
fi

# A Docker action runs as root, so every file written above belongs to root and
# the runner user cannot touch it: a workflow appending a download link to the
# summary fails with "Permission denied". Hand the outputs back to whoever owns
# the workspace, which is the user the rest of the job runs as.
for path in "$SUMMARY" "$SARIF" "$BRIEF_DIR" "$BRIEF_ZIP"; do
  [ -e "$path" ] || continue
  chown -R --reference="${GITHUB_WORKSPACE:-.}" "$path" 2>/dev/null || true
done

# A Docker action runs as root, so every file written above belongs to root and
# the runner user cannot touch it: a workflow appending a download link to the
# summary fails with "Permission denied". Hand the outputs back to whoever owns
# the workspace, which is the user the rest of the job runs as.
for path in "$SUMMARY" "$SARIF" "$BRIEF_DIR" "$BRIEF_ZIP"; do
  [ -e "$path" ] || continue
  chown -R --reference="${GITHUB_WORKSPACE:-.}" "$path" 2>/dev/null || true
done

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
    echo "brief=$BRIEF_MD"
    echo "brief-zip=$BRIEF_ZIP"
    echo "brief-dir=$BRIEF_DIR"
  } >> "$GITHUB_OUTPUT"
fi

# The job summary renders on the run page with no extra configuration, so there
# is something to read even before SARIF upload or PR comments are wired up.
if [ -n "${GITHUB_STEP_SUMMARY:-}" ] && [ -f "$SUMMARY" ]; then
  cat "$SUMMARY" >> "$GITHUB_STEP_SUMMARY"
fi

log "findings: $TOTAL (critical $CRITICAL, high $HIGH)"
exit $STATUS
