#!/usr/bin/env bash
# Shuts down and starts each worker VM, one at a time, so a change to its Proxmox hardware (eg memory) applies: a
# reboot from inside the VM, like the daily 05:20 one, leaves such a change pending. Run as root on pve3:
#   bash restart-worker-vms.sh                # the workers 341 to 347
#   bash restart-worker-vms.sh 345 346 347    # just these
#
# A job running on a VM as it shuts down is killed, and retried once its lease expires (about 7 min), using one of its
# attempts. So run it when the queue is quiet.
set -euo pipefail

VMIDS="${*:-341 342 343 344 345 346 347}"

for vmid in $VMIDS; do
    name="$(qm config "$vmid" | sed -n 's/^name: //p')"
    memory="$(qm config "$vmid" | sed -n 's/^memory: //p')"
    # only ever a worker, so a mistyped id can't take down the coordinator or the LLM VM
    # lower-cased, as Proxmox has them as eg GLANWORKER1-41
    if [[ "${name,,}" != *worker* ]]; then
        echo "VM $vmid is '$name', not a worker - stopping here" >&2
        exit 1
    fi

    echo "== $name (VM $vmid): shutting down"
    # a clean shutdown, as the guest's systemd stops the supervisor. Fails, rather than pulling the plug, after 5 min
    qm shutdown "$vmid" --timeout 300
    qm start "$vmid"
    echo "== $name (VM $vmid): started with $memory MB"

    # give it time to boot and its supervisor to start before taking down the next one
    sleep 60
done

echo "Done. Check each worker's RAM with: free -m"
