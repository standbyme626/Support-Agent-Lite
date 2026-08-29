#!/usr/bin/env bash
# Daily consistent backup of the production SQLite DB via the online
# .backup API (safe while the server is running). Keeps the last 7 copies.
# Cron: 30 3 * * *  <project>/scripts/ops/backup_db.sh
set -eu
BASE_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
DB="${SUPPORT_AGENT_DB:-$BASE_DIR/runtime/support_agent.db}"
BACKUP_DIR="$BASE_DIR/runtime/backups"
PYTHON="$BASE_DIR/.venv/bin/python"
mkdir -p "$BACKUP_DIR"

STAMP="$(date '+%Y%m%d')"
OUT="$BACKUP_DIR/support_agent_$STAMP.db"

"$PYTHON" - "$DB" "$OUT" <<'PY'
import sqlite3
import sys

src, dst = sys.argv[1], sys.argv[2]
source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
target = sqlite3.connect(dst)
with target:
    source.backup(target)
target.close()
source.close()
PY

# retention: newest 7
ls -1t "$BACKUP_DIR"/support_agent_*.db 2>/dev/null | tail -n +8 | xargs -r rm -f
echo "$(date '+%F %T') backed up $DB -> $OUT"
