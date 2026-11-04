#!/bin/bash
# Deploys main to each worker VM over SSH, one at a time: kill the running job, stop, pull, sync, start.
# Deploy the coordinator first if there's a new migration, as workers don't migrate.
#
#   bash scripts/job_queue/deploy-workers.sh

set -euo pipefail

HOST=192.168.1.212
PORTS="341 342"     # worker41, worker42

# The workers pull from GitHub, so refuse to deploy anything that isn't committed and pushed
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
CONFIGS="secrets/orchestration-aa-dave-testing.yaml secrets/orchestration-aa-clienta.yaml secrets/orchestration-aa-clientb.yaml"
for CONFIG_PORT in 340 341 342; do
    echo "== Copying $CONFIGS to $HOST port $CONFIG_PORT"
    scp -q -P "$CONFIG_PORT" $CONFIGS "$HOST:/home/dave/auto-archiver/secrets/"
done

for PORT in $PORTS; do
    echo "######## $HOST port $PORT"
    ssh -p "$PORT" "$HOST" bash -s <<'REMOTE'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd /home/dave/auto-archiver

echo "== $(hostname): stopping aa-worker-supervisor now, killing any running job"
# the killed job is retried once its lease expires, and the supervisor clears leftover containers when it starts.
# stop after the kill, or Restart=on-failure would start it again mid-deploy
sudo systemctl kill -s SIGKILL aa-worker-supervisor || true
sudo systemctl stop aa-worker-supervisor

echo "== Pulling main"
git pull origin main
git log -1 --oneline

echo "== Syncing packages"
poetry sync --only main

echo "== Starting aa-worker-supervisor"
sudo systemctl start aa-worker-supervisor
sleep 10
systemctl is-active aa-worker-supervisor
journalctl -u aa-worker-supervisor -n 20 --no-pager
REMOTE
done
