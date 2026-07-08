#!/usr/bin/env bash
# Verschlüsseltes Backup der SQLite-DB (konsistent via sqlite3 .backup).
#
# Benötigt auf dem Host: sqlite3, age   (apt install sqlite3 age)
# Konfiguration über deploy.env: BACKUP_AGE_RECIPIENT (Pflicht),
# BACKUP_KEEP_DAYS, BACKUP_RSYNC_TARGET (optional).
#
# Cron-Beispiel (stündlich):
#   0 * * * * /opt/camp-doku/scripts/backup.sh >> /var/log/camp-doku-backup.log 2>&1
#
# Wiederherstellen (auf dem eigenen Laptop, mit dem privaten Schlüssel):
#   age -d -i camp-doku-backup-key.txt -o app.db app-2026-07-20_14-00.db.age

set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f deploy.env ]; then
    set -a
    # shellcheck disable=SC1091
    source deploy.env
    set +a
fi

: "${BACKUP_AGE_RECIPIENT:?BACKUP_AGE_RECIPIENT fehlt (age public key, siehe deploy.env.example)}"
: "${BACKUP_KEEP_DAYS:=14}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
DB_FILE="${DB_FILE:-./data/app.db}"

[ -f "$DB_FILE" ] || { echo "FEHLER: $DB_FILE nicht gefunden."; exit 1; }
mkdir -p "$BACKUP_DIR"

ts=$(date +%Y-%m-%d_%H-%M)
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT

# .backup ist auch bei laufender App konsistent (nutzt SQLite-Locking)
sqlite3 "$DB_FILE" ".backup '$tmp'"
age -r "$BACKUP_AGE_RECIPIENT" -o "$BACKUP_DIR/app-$ts.db.age" "$tmp"

# Rotation
find "$BACKUP_DIR" -name 'app-*.db.age' -mtime +"$BACKUP_KEEP_DAYS" -delete

# Optional off-site
if [ -n "${BACKUP_RSYNC_TARGET:-}" ]; then
    rsync -az "$BACKUP_DIR/" "$BACKUP_RSYNC_TARGET/"
fi

echo "$(date '+%F %T') Backup ok: $BACKUP_DIR/app-$ts.db.age ($(du -h "$BACKUP_DIR/app-$ts.db.age" | cut -f1))"
