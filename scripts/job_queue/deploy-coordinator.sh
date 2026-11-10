#!/bin/bash
# Deploys main to the coordinator VM over SSH: stop, pull, sync, back up the database, migrate, start.
# Then registers and turns on any config in CONFIGS that isn't on the queue yet.
# Also makes sure the nightly database backup (backup-db.sh) and the morning report (job_queue/report.py) are in the
# coordinator's crontab.
# Also copies the alert email settings (POSTMARK_API_TOKEN, POSTMARK_FROM_EMAIL, ALERT_EMAIL_TO) from your .env.
# Packages come from the committed poetry.lock: run poetry update locally, test and push, never on the VM.
# --reset-except-sheets also empties every queue table except sheets (and schema_migrations), to start again.
#
#   bash scripts/job_queue/deploy-coordinator.sh [--reset-except-sheets]

set -euo pipefail

HOST=192.168.1.212
PORT=340

RESET=0
if [ "${1:-}" = "--reset-except-sheets" ]; then
    RESET=1
elif [ $# -gt 0 ]; then
    echo "Usage: $0 [--reset-except-sheets]"
    exit 1
fi

# The coordinator pulls from GitHub, so refuse to deploy anything that isn't committed and pushed
cd "$(dirname "$0")/../.."
if [ -n "$(git status --porcelain)" ]; then
    echo "Uncommitted changes. Commit and push them first:" >&2
    git status --short >&2
    exit 1
fi
git fetch -q origin main
if [ -n "$(git log origin/main..HEAD --oneline)" ]; then
    echo "Commits not pushed to origin/main. Push them first:" >&2
    git log origin/main..HEAD --oneline >&2
    exit 1
fi

# secrets/ isn't in git. The coordinator writes the sheet cells using its copy's columns, and the workers archive with
# theirs, so copy them to all three whichever script is run (DM 4th Nov 26 - a stale coordinator copy dropped new columns)
CONFIGS="secrets/orchestration-aa-dave-testing.yaml secrets/orchestration-aa-clienta.yaml secrets/orchestration-aa-clientb.yaml secrets/orchestration-glan-sheet1.yaml secrets/orchestration-glan-sheet4.yaml secrets/orchestration-cir-indigo.yaml secrets/orchestration-cir-domain-eor.yaml"
for CONFIG_PORT in 340 341 342 343 344 345 346; do
    echo "== Copying $CONFIGS to $HOST port $CONFIG_PORT"
    scp -q -P "$CONFIG_PORT" $CONFIGS "$HOST:/home/dave/auto-archiver/secrets/"
done

# DM 10th Nov 26 - the Google service accounts the configs use. Only the coordinator needs them, as it reads and writes
# the sheets. rsync, so only changed ones are sent
echo "== Copying any changed service accounts to $HOST port $PORT"
rsync -tp --itemize-changes -e "ssh -p $PORT" secrets/service_account*.json "$HOST:/home/dave/auto-archiver/secrets/"

# DM 9th Nov 26 - the alert email settings from your .env, but not the whole file, as its AA_DATABASE_URL is your local
# database. They go on stdin, so they're never on a command line or in the output
for KEY in POSTMARK_API_TOKEN POSTMARK_FROM_EMAIL ALERT_EMAIL_TO; do
    if ! grep -q "^$KEY=." .env 2>/dev/null; then
        echo "No $KEY in your .env, which the coordinator needs to email alerts" >&2
        exit 1
    fi
done
echo "== Copying the alert email settings from .env to $HOST port $PORT"
# on the coordinator: keep its AA_DATABASE_URL and anything else, and replace any old copies of the three settings
grep -E "^(POSTMARK_API_TOKEN|POSTMARK_FROM_EMAIL|ALERT_EMAIL_TO)=" .env | ssh -p "$PORT" "$HOST" 'set -euo pipefail
cd /home/dave/auto-archiver
grep -q "^AA_DATABASE_URL=" .env
{ grep -vE "^(POSTMARK_API_TOKEN|POSTMARK_FROM_EMAIL|ALERT_EMAIL_TO)=" .env || true; cat; } > .env.new
chmod 600 .env.new
mv .env.new .env
echo "   its .env now has: $(grep -oE "^[A-Z_]+" .env | tr "\n" " ")"'

ssh -p "$PORT" "$HOST" RESET=$RESET CONFIGS="'$CONFIGS'" bash -s <<'REMOTE'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd /home/dave/auto-archiver

echo "== Stopping aa-coordinator"
sudo systemctl stop aa-coordinator

echo "== Pulling main"
git pull origin main
git log -1 --oneline

echo "== Syncing packages"
# sync, not install, so packages dropped from poetry.lock are removed too
poetry sync --only main

# before a migration or --reset-except-sheets changes anything. pg_dump takes a consistent snapshot, so the workers can run
echo "== Backing up the database"
bash scripts/job_queue/backup-db.sh

# adds a cron line if it isn't there, replacing an older one that contains $2, and keeping any other lines
cron_line() {
    local line="$1" marker="$2" existing
    existing="$(crontab -l 2>/dev/null || true)"
    if grep -qxF "$line" <<<"$existing"; then
        echo "already in the crontab"
    else
        { grep -vF -e "$marker" <<<"$existing" | grep -v '^$' || true; echo "$line"; } | crontab -
        echo "added to the crontab: $line"
    fi
}

echo "== Nightly backup at 02:30 UTC"
mkdir -p "$HOME/backups"
cron_line "30 2 * * * bash $PWD/scripts/job_queue/backup-db.sh >> $HOME/backups/backup.log 2>&1" "backup-db.sh"

echo "== Morning report at 05:00 UTC"
mkdir -p "$HOME/reports"
cron_line "0 5 * * * cd $PWD && $HOME/.local/bin/poetry run python -m auto_archiver.job_queue.report >> $HOME/reports/report.log 2>&1" "auto_archiver.job_queue.report"

echo "== Migrating"
poetry run python -m auto_archiver.job_queue.migrate

if [ "$RESET" = "1" ]; then
    echo "== Emptying every table except sheets"
    PGPASSWORD=password psql -h localhost -U bob -d auto_archiver -v ON_ERROR_STOP=1 -c \
        "TRUNCATE job_files, sheet_writes, jobs, drive_folders, admin_alerts, heartbeats RESTART IDENTITY"
fi

echo "== Starting aa-coordinator"
sudo systemctl start aa-coordinator
sleep 10
systemctl is-active aa-coordinator
journalctl -u aa-coordinator -n 20 --no-pager

# DM 10th Nov 26 - a config new to CONFIGS goes straight on the queue. One already there, even if off, is left alone
echo "== Adding any new config to the queue"
poetry run python -m auto_archiver.job_queue.sync add $CONFIGS
REMOTE
