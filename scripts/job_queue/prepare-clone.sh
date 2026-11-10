#!/usr/bin/env bash
# Run once on a VM just cloned from the Ubuntu 26.04 template (VM 1001), before install-worker.sh, then reboot.
# The template was installed from the ISO, so cloud-init is off and a clone keeps the template's hostname, machine-id
# and SSH host keys, and / uses 15 GB of the disk. The hostname is the worker's name on the queue.
#
#   bash scripts/job_queue/prepare-clone.sh <worker_name>
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: bash scripts/job_queue/prepare-clone.sh <worker_name>" >&2
    exit 1
fi
name="$1"
old="$(hostname)"

echo "=== Hostname $old -> $name"
sudo hostnamectl set-hostname "$name"
sudo sed -i "s/\b$old\b/$name/g" /etc/hosts

echo "=== New machine-id, which is also its DHCP client id, so it doesn't get another clone's address"
sudo rm -f /etc/machine-id
sudo systemd-machine-id-setup

echo "=== New SSH host keys"
sudo rm -f /etc/ssh/ssh_host_*
sudo ssh-keygen -A

echo "=== / to fill the disk"
# growpart exits 1 when the partition already fills the disk
sudo growpart /dev/sda 3 || true
sudo pvresize /dev/sda3
if [ "$(sudo vgs --noheadings -o vg_free_count ubuntu-vg | tr -d ' ')" -gt 0 ]; then
    sudo lvextend -r -l +100%FREE /dev/ubuntu-vg/ubuntu-lv
fi
df -h /

echo "Done. Reboot (sudo reboot) so it gets its new address, then run install-worker.sh"
