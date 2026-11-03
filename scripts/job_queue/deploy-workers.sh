#!/bin/bash
# Deploys main to each worker VM over SSH, one at a time: let the running job finish, stop, pull, sync, start.
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

for PORT in $PORTS; do
    echo "######## $HOST port $PORT"
    ssh -p "$PORT" "$HOST" bash -s <<'REMOTE'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd /home/dave/auto-archiver

echo "== $(hostname): stopping aa-worker-supervisor once its running job finishes"
if systemctl is-active --quiet aa-worker-supervisor; then
    sudo systemctl kill --kill-who=main -s SIGTERM aa-worker-supervisor
fi
while systemctl is-active --quiet aa-worker-supervisor; do
    echo "   still running a job, waiting..."
    sleep 30
done

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
