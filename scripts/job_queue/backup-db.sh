#!/bin/bash
# Backs up the queue database on the coordinator, to ~/backups/auto_archiver_<weekday>.dump, so it keeps 7 days.
# Run nightly by cron (deploy-coordinator.sh adds the line), and by each deploy before it migrates.
# Uses AA_DATABASE_URL from the repo's .env, so no password is in the crontab.
#
#   bash scripts/job_queue/backup-db.sh
#
# To restore a backup over the current database (stop aa-coordinator and the workers first):
#   pg_restore --clean --if-exists --no-owner -d "<AA_DATABASE_URL>" ~/backups/auto_archiver_<weekday>.dump

set -euo pipefail

cd "$(dirname "$0")/../.."
URL="$(grep '^AA_DATABASE_URL=' .env | cut -d= -f2-)"
BACKUP_DIR="$HOME/backups"
FILE="$BACKUP_DIR/auto_archiver_$(date +%a).dump"

mkdir -p "$BACKUP_DIR"
# to a temporary file first, so a failed dump never replaces a good one
pg_dump --format=custom --file="$FILE.tmp" "$URL"
mv "$FILE.tmp" "$FILE"
echo "$(date -u '+%Y-%m-%d %H:%M:%S') UTC backed up to $FILE ($(du -h "$FILE" | cut -f1))"
