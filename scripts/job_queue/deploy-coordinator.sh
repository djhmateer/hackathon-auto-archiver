#!/bin/bash
# Deploys main to the coordinator VM over SSH: stop, pull, install, migrate, start.
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
CONFIGS="secrets/orchestration-aa-dave-testing.yaml secrets/orchestration-aa-clienta.yaml"
for CONFIG_PORT in 340 341 342; do
    echo "== Copying $CONFIGS to $HOST port $CONFIG_PORT"
    scp -q -P "$CONFIG_PORT" $CONFIGS "$HOST:/home/dave/auto-archiver/secrets/"
done

ssh -p "$PORT" "$HOST" RESET=$RESET bash -s <<'REMOTE'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd /home/dave/auto-archiver

echo "== Stopping aa-coordinator"
sudo systemctl stop aa-coordinator

echo "== Pulling main"
git pull origin main
git log -1 --oneline

echo "== Installing"
poetry install --only main

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
REMOTE
