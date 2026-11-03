#!/usr/bin/env bash
# Sets up a worker VM on Ubuntu 26.04: browsers, Docker, Python 3.12, the project and the service.
# Safe to run again. Doesn't start the worker supervisor, which needs the secrets first.
#
# As your normal user, from a clone of the repo:
#   git pull origin main
#   bash scripts/job_queue/install-worker.sh --coordinator-database <coordinator_ip>
set -euo pipefail

DB_NAME=auto_archiver
DB_USER=bob
DB_PASSWORD=password
GECKODRIVER_VERSION=0.37.1

if [ $# -ne 2 ] || [ "$1" != "--coordinator-database" ]; then
    echo "Usage: bash scripts/job_queue/install-worker.sh --coordinator-database <coordinator_ip>" >&2
    exit 1
fi
COORDINATOR_IP="$2"

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user, not root. It uses sudo where needed." >&2
    exit 1
fi

echo "=== System packages"
sudo apt-get update
# exiftool, ffmpeg and tesseract are used by enrichers. The fonts let screenshots render other languages
sudo apt-get install -y git curl wget unzip ca-certificates gnupg build-essential libpq5 \
    ffmpeg libimage-exiftool-perl tesseract-ocr fonts-noto
sudo install -m 0755 -d /etc/apt/keyrings

echo "=== Firefox and geckodriver, for screenshots"
# Ubuntu's own firefox package is a snap, so use Mozilla's, and pin it above Ubuntu's
wget -qO- https://packages.mozilla.org/apt/repo-signing-key.gpg | sudo tee /etc/apt/keyrings/packages.mozilla.org.asc >/dev/null
echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" |
    sudo tee /etc/apt/sources.list.d/mozilla.list >/dev/null
printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' | sudo tee /etc/apt/preferences.d/mozilla >/dev/null
sudo apt-get update
sudo apt-get install -y firefox

if ! command -v geckodriver >/dev/null; then
    wget -qO- "https://github.com/mozilla/geckodriver/releases/download/v$GECKODRIVER_VERSION/geckodriver-v$GECKODRIVER_VERSION-linux64.tar.gz" |
        sudo tar -xz -C /usr/local/bin geckodriver
    sudo chmod 755 /usr/local/bin/geckodriver
fi

echo "=== Chrome, for the antibot and mhtml modules"
wget -qO- https://dl.google.com/linux/linux_signing_key.pub | sudo gpg --dearmor --yes -o /etc/apt/keyrings/google-chrome.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" |
    sudo tee /etc/apt/sources.list.d/google-chrome.list >/dev/null
sudo apt-get update
sudo apt-get install -y google-chrome-stable

echo "=== Docker, for browsertrix and YouTube tokens"
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
codename="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")"
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $codename stable" |
    sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

# sudo, as the docker group only applies from the next login
sudo docker pull webrecorder/browsertrix-crawler:latest
if ! sudo docker inspect bgutil-provider >/dev/null 2>&1; then
    sudo docker run --name bgutil-provider --restart unless-stopped -d -p 4416:4416 brainicism/bgutil-ytdlp-pot-provider
fi

echo "=== Deno, for yt-dlp"
[ -x "$HOME/.deno/bin/deno" ] || curl -fsSL https://deno.land/install.sh | sh -s -- -y

echo "=== Python 3.12 and Poetry"
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12
uv tool install "poetry>=2.0.0,<3.0.0"
poetry env use "$(uv python find 3.12)"
poetry install --only main

echo "=== Connection string and folders"
# always written, so running this again with a corrected IP fixes it
echo "AA_DATABASE_URL=postgresql://$DB_USER:$DB_PASSWORD@$COORDINATOR_IP:5432/$DB_NAME" > .env
chmod 600 .env
mkdir -p secrets "$HOME/aatmp"

echo "=== Checking Postgres on the coordinator"
if ! poetry run python -c "
import sys
from auto_archiver.job_queue import db, migrate
with db.connect() as conn:
    pending, problems = migrate.check(conn)
sys.exit(1 if pending or problems else 0)
"; then
    echo "Can't use the database at $COORDINATOR_IP. Check port 5432, its pg_hba samenet line, .env, and that it's migrated." >&2
    exit 1
fi
echo "Connected, and migrations match"

echo "=== Daily reboot"
sudoers_line="$USER ALL=(root) NOPASSWD: /sbin/reboot"
echo "$sudoers_line" | sudo tee /etc/sudoers.d/aa-reboot >/dev/null
sudo chmod 440 /etc/sudoers.d/aa-reboot
sudo visudo -cf /etc/sudoers.d/aa-reboot

echo "=== Service"
sed -e "s#/home/dave/auto-archiver#$REPO#g" -e "s#/home/dave#$HOME#g" -e "s#^User=dave#User=$USER#" \
    scripts/job_queue/aa-worker-supervisor.service | sudo tee /etc/systemd/system/aa-worker-supervisor.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable aa-worker-supervisor

cat <<DONE

Done. The service is enabled, so it starts by itself on the reboot. Still to do by hand, in this order:
  1. Copy into $REPO/secrets/ every config the queue runs, with its service accounts, cookies, sessions and profiles.
     Do this before rebooting: without the configs every job fails in setup
  2. Check the versions look right:
       firefox --version; geckodriver --version; google-chrome --version; deno --version
  3. Reboot, so the docker group applies. The worker starts by itself:
       sudo reboot
  4. Watch it. Look for "Started worker $(hostname)", and "Worker $(hostname) is up" on the coordinator:
       journalctl -u aa-worker-supervisor -f
DONE
