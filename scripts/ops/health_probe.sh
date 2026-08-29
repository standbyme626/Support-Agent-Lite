#!/usr/bin/env bash
# Health probe for the production API (cron: every minute, as root).
# On failure: log an alert line AND attempt one service restart, so a hung
# or dead API self-heals instead of silently dropping Feishu traffic.
set -u
BASE_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
URL="${SUPPORT_AGENT_HEALTH_URL:-http://127.0.0.1:8322/health}"
LOG_DIR="$BASE_DIR/runtime/ops"
UNIT="${SUPPORT_AGENT_API_UNIT:-support-agent-api}"
mkdir -p "$LOG_DIR"

if curl -sf -m 5 "$URL" >/dev/null 2>&1; then
  exit 0
fi

{
  echo "$(date '+%F %T') DOWN $URL"
  systemctl restart "$UNIT" 2>&1 || true
  sleep 3
  if curl -sf -m 5 "$URL" >/dev/null 2>&1; then
    echo "$(date '+%F %T') RESTARTED $UNIT -> healthy"
  else
    echo "$(date '+%F %T') RESTART FAILED $UNIT still down"
  fi
} >> "$LOG_DIR/health_alerts.log"
