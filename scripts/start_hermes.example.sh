#!/usr/bin/env bash
# Reference launcher for Hermès (READ-ONLY, HUMAN_APPROVAL). Copy to scripts/start_hermes.sh
# (kept untracked) and adapt paths if needed. This file contains no secret and must never get one.
#
#   scripts/start_hermes.sh                  # live run until Ctrl-C
#   scripts/start_hermes.sh --duration 120   # smoke test
#   HERMES_WARM_START=0 scripts/start_hermes.sh
#
# - never prints the environment (no `set -x`, no `env`, no `cat .env.local`);
# - refuses to start if the safety invariants in config/hermes.toml are not what V1 requires;
# - `exec`s Python so Ctrl-C / SIGTERM / SIGUSR1 reach Hermès directly (clean shutdown + summary).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# 1. virtualenv
if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
else
  echo "start_hermes: .venv not found in $REPO" >&2
  exit 2
fi

# 2. local secrets (Slack / OpenAI / approver IDs) — loaded, never echoed
ENV_FILE="${HERMES_ENV_FILE:-$REPO/.env.local}"
if [[ -f "$ENV_FILE" ]]; then
  if stat -c '%a' "$ENV_FILE" >/dev/null 2>&1; then perms="$(stat -c '%a' "$ENV_FILE")"   # GNU
  else perms="$(stat -f '%Lp' "$ENV_FILE")"; fi                                             # macOS
  if [[ "$perms" != "600" && "$perms" != "400" ]]; then
    echo "start_hermes: $ENV_FILE should be chmod 600 (is $perms)" >&2
  fi
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

# 3. safety invariants (V1: read-only, no orders, human approval only)
CFG="${HERMES_CONFIG:-$REPO/config/hermes.toml}"
check() {  # $1 = regex that must match exactly one active line
  if [[ "$(grep -cE "$1" "$CFG")" != "1" ]]; then
    echo "start_hermes: refusing to start, expected '$1' in $CFG" >&2
    exit 3
  fi
}
check '^orders_enabled[[:space:]]*=[[:space:]]*false([[:space:]]|#|$)'
check '^read_only[[:space:]]*=[[:space:]]*true([[:space:]]|#|$)'
check '^mode[[:space:]]*=[[:space:]]*"HUMAN_APPROVAL"'

echo "start_hermes: $(git log -1 --oneline 2>/dev/null || echo 'no git') | warm start=${HERMES_WARM_START:-1} | slack=$([[ -n "${SLACK_BOT_TOKEN:-}" ]] && echo configured || echo off) | approvers=$([[ -n "${HERMES_SLACK_APPROVER_IDS:-}" ]] && echo configured || echo 'not configured')"
echo "start_hermes: logs in ~/hermes-data/logs (hermes.log, hermes-telemetry.jsonl); Ctrl-C to stop"

# 4. keep the Mac awake for the run (macOS), and hand the process over to Python.
#    Ctrl-C reaches Hermès through the terminal's process group. For SIGTERM / SIGUSR1 (operator
#    retry) signal the Python process itself:  kill -USR1 "$(pgrep -f hermes.app.run_live)"
if command -v caffeinate >/dev/null 2>&1; then
  exec caffeinate -dimsu python -m hermes.app.run_live --config "$CFG" "$@"
fi
exec python -m hermes.app.run_live --config "$CFG" "$@"
