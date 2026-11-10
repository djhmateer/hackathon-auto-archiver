#!/usr/bin/env bash
# Makes a worker VM on the Proxmox host by cloning the Ubuntu 26.04 cloud-init template. Run as root on pve3:
#   bash new-worker-vm.sh <new_vmid> <name>
#   STORAGE=vm-storage bash new-worker-vm.sh <new_vmid> <name>    # its disks on another storage
#
# Name it as the worker, eg hmsworker2. The template was installed from the ISO, so the clone is still a copy of it:
# then run scripts/job_queue/prepare-clone.sh on it, for its hostname, machine-id, SSH host keys and disk size.
set -euo pipefail

TEMPLATE_ID="${TEMPLATE_ID:-1001}"  # ubuntu-26-04
LIKE_ID="${LIKE_ID:-343}"           # an existing worker, for its memory and cores
STORAGE="${STORAGE:-}"              # eg vm-storage. Empty: the same storage as the template's disks
DISK_SIZE=64G

if [ $# -ne 2 ]; then
    echo "Usage: bash new-worker-vm.sh <new_vmid> <name>" >&2
    exit 1
fi
vmid="$1"
name="$2"

like="$(qm config "$LIKE_ID")"
memory="$(echo "$like" | sed -n 's/^memory: //p')"
cores="$(echo "$like" | sed -n 's/^cores: //p')"

# a full clone, so it doesn't depend on the template's disk
qm clone "$TEMPLATE_ID" "$vmid" --name "$name" --full ${STORAGE:+--storage "$STORAGE"}
qm set "$vmid" --memory "$memory" --cores "$cores"
# prepare-clone.sh then grows / to fill it
qm resize "$vmid" scsi0 "$DISK_SIZE"
qm start "$vmid"
echo "Started $name (VM $vmid): $memory MB, $cores cores, $DISK_SIZE disk," \
    "MAC $(qm config "$vmid" | sed -n 's/^net0: [a-z0-9]*=\([^,]*\).*/\1/p')"
