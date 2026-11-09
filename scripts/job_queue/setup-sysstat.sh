#!/bin/bash
# Machine history for the log analysis (aa-logs-pve3) and the morning report: sysstat sampling every minute instead
# of every 10, so short memory and CPU spikes show; 28 days kept, as the logs are imported daily; -S XALL adds memory,
# CPU and IO pressure (PSI), about 240 MB for the 28 days. Safe to run again. Run on each VM, as your normal user:
#   bash scripts/job_queue/setup-sysstat.sh
# Called by install-coordinator.sh and install-worker.sh.
set -euo pipefail

sudo apt-get install -y -q sysstat >/dev/null
sudo sed -i 's/^ENABLED=.*/ENABLED="true"/' /etc/default/sysstat
sudo sed -i -E -e 's/^HISTORY=.*/HISTORY=28/' -e 's/^SADC_OPTIONS=.*/SADC_OPTIONS="-S XALL"/' /etc/sysstat/sysstat
sudo mkdir -p /etc/systemd/system/sysstat-collect.timer.d
# the empty OnCalendar= clears the package's every-10-minutes one
printf '%s\n' '[Timer]' 'OnCalendar=' 'OnCalendar=*:*:00' |
    sudo tee /etc/systemd/system/sysstat-collect.timer.d/zz-every-minute.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable -q --now sysstat sysstat-collect.timer
# picks up the new timer and options now, not only after the next reboot
sudo systemctl restart sysstat-collect.timer
echo "sysstat: every minute, 28 days kept, -S XALL"
