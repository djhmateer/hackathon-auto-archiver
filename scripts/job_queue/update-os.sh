#!/bin/bash
# Upgrades the Ubuntu packages (including Firefox, Chrome and Docker from their own apt repos) on the coordinator, then
# each worker, one at a time, and reboots each. Separate from the deploys, so a code problem and an OS one aren't mixed.
# Run it when the queue is quiet:
#   - a worker's running job is killed, and retried once its lease expires
#   - while the coordinator reboots no jobs are claimed, and the workers retry their database writes for about 5 min
#
#   bash scripts/job_queue/update-os.sh

set -euo pipefail

HOST=192.168.1.212

update() {
    local port=$1 service=$2
    echo "######## $HOST port $port"

    ssh -p "$port" "$HOST" SERVICE="$service" bash -s <<'REMOTE'
set -euo pipefail
echo "== $(hostname): stopping $SERVICE"
if [ "$SERVICE" = "aa-worker-supervisor" ]; then
    # as deploy-workers.sh: kill the running job, then stop, or Restart=on-failure would start it again
    sudo systemctl kill -s SIGKILL "$SERVICE" || true
fi
sudo systemctl stop "$SERVICE"

echo "== Upgrading packages"
sudo apt-get update
# no prompts: keep our edited config files, and leave restarting services to the reboot rather than needrestart.
# --with-new-pkgs installs new dependencies (eg a new kernel) but never removes anything
sudo env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get -y --with-new-pkgs \
    -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade
REMOTE

    echo "== Rebooting"
    local boot_id new_boot_id=""
    boot_id="$(ssh -p "$port" "$HOST" cat /proc/sys/kernel/random/boot_id)"
    # the connection drops as it goes down, so an error from ssh here is expected
    ssh -p "$port" "$HOST" sudo -n /sbin/reboot || true

    # a new boot id means it has rebooted and is back, rather than not gone down yet
    for _ in $(seq 60); do
        sleep 5
        new_boot_id="$(ssh -o ConnectTimeout=5 -p "$port" "$HOST" cat /proc/sys/kernel/random/boot_id 2>/dev/null)" || continue
        [ "$new_boot_id" != "$boot_id" ] && break
    done
    if [ -z "$new_boot_id" ] || [ "$new_boot_id" = "$boot_id" ]; then
        echo "Port $port hasn't come back from its reboot after 5 minutes. Stopping here" >&2
        exit 1
    fi

    ssh -p "$port" "$HOST" SERVICE="$service" bash -s <<'REMOTE'
set -euo pipefail
echo "== $(hostname) is back: waiting for $SERVICE, which starts itself"
for _ in $(seq 30); do
    systemctl is-active --quiet "$SERVICE" && break
    sleep 2
done
systemctl is-active "$SERVICE"

echo "== Versions"
uname -r
if [ "$SERVICE" = "aa-worker-supervisor" ]; then
    firefox --version
    google-chrome --version
    docker --version
    docker ps --filter name=bgutil-provider --format "bgutil-provider: {{.Image}} {{.Status}}"
else
    psql --version
fi
journalctl -u "$SERVICE" -n 10 --no-pager
REMOTE
}

update 340 aa-coordinator
update 341 aa-worker-supervisor
update 342 aa-worker-supervisor
update 343 aa-worker-supervisor
update 344 aa-worker-supervisor
update 345 aa-worker-supervisor
