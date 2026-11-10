#!/usr/bin/env bash
# From your machine, for a VM just made by new-worker-vm.sh, once its SSH port forward is set up: copies your SSH key
# to it, runs prepare-clone.sh on it (hostname, machine-id, SSH host keys, disk size) and reboots it.
# Asks for the VM's password (the template's) for the key, then for sudo.
#
#   bash scripts/job_queue/setup-clone.sh <port> <worker_name>
set -euo pipefail

HOST=192.168.1.212
KEY="${KEY:-$HOME/.ssh/id_ed25519.pub}"

if [ $# -ne 2 ]; then
    echo "Usage: bash scripts/job_queue/setup-clone.sh <port> <worker_name>" >&2
    exit 1
fi
port="$1"
name="$2"
cd "$(dirname "$0")/../.."

ssh-copy-id -i "$KEY" -p "$port" "dave@$HOST"
scp -P "$port" scripts/job_queue/prepare-clone.sh "dave@$HOST:"
status=0
ssh -t -p "$port" "dave@$HOST" "bash prepare-clone.sh $name && rm prepare-clone.sh && sudo reboot" || status=$?
# 255 is the connection dropping as it reboots
if [ "$status" -ne 0 ] && [ "$status" -ne 255 ]; then
    exit "$status"
fi

# prepare-clone.sh made new SSH host keys, so forget the template's
ssh-keygen -R "[$HOST]:$port"
echo "Rebooting. In a minute: ssh $HOST -p $port, accept its new key, check hostname and machine-id, then install"
