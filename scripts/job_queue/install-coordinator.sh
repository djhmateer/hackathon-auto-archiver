#!/usr/bin/env bash
# Sets up the coordinator VM on Ubuntu 26.04: Postgres 18, Python 3.12, the project, the database and the service.
# Safe to run again. Doesn't start the coordinator, which needs the secrets first.
#
# As your normal user, from a clone of the repo:
#   git pull origin main
#   bash scripts/job_queue/install-coordinator.sh
set -euo pipefail

DB_NAME=auto_archiver
DB_USER=bob
DB_PASSWORD=password

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user, not root. It uses sudo where needed." >&2
    exit 1
fi

echo "=== System packages"
sudo apt-get update
if [ -z "$(apt-cache policy postgresql-18 | awk '/Candidate:/ {print $2}' | grep -v '(none)')" ]; then
    echo "postgresql-18 isn't in this VM's apt sources. Add the PostgreSQL apt repo, then run this again." >&2
    exit 1
fi
sudo apt-get install -y git curl build-essential libpq5 postgresql-18

echo "=== Python 3.12 and Poetry"
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12
uv tool install "poetry>=2.0.0,<3.0.0"
poetry env use "$(uv python find 3.12)"
poetry install --only main

echo "=== Postgres"
as_postgres() { sudo -u postgres psql -v ON_ERROR_STOP=1 -tA "$@"; }

sudo systemctl enable --now postgresql
as_postgres -c "ALTER SYSTEM SET listen_addresses = '*'"
hba_file="$(as_postgres -c 'SHOW hba_file')"
# samenet: only subnets this VM is directly attached to
hba_line="host $DB_NAME $DB_USER samenet scram-sha-256"
sudo grep -qxF "$hba_line" "$hba_file" || echo "$hba_line" | sudo tee -a "$hba_file" >/dev/null
sudo systemctl restart postgresql

if [ "$(as_postgres -c "SELECT 1 FROM pg_roles WHERE rolname = '$DB_USER'")" != "1" ]; then
    as_postgres -c "CREATE ROLE $DB_USER LOGIN"
fi
as_postgres -c "ALTER ROLE $DB_USER PASSWORD '$DB_PASSWORD'" >/dev/null
if [ "$(as_postgres -c "SELECT 1 FROM pg_database WHERE datname = '$DB_NAME'")" != "1" ]; then
    as_postgres -c "CREATE DATABASE $DB_NAME OWNER $DB_USER"
fi

echo "=== Connection string"
if [ ! -f .env ]; then
    echo "AA_DATABASE_URL=postgresql://$DB_USER:$DB_PASSWORD@localhost:5432/$DB_NAME" > .env
fi
chmod 600 .env

echo "=== Migrations"
poetry run python -m auto_archiver.job_queue.migrate

echo "=== Service"
sed -e "s#/home/dave/auto-archiver#$REPO#g" -e "s#/home/dave#$HOME#g" -e "s#^User=dave#User=$USER#" \
    scripts/job_queue/aa-coordinator.service | sudo tee /etc/systemd/system/aa-coordinator.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable aa-coordinator

echo "=== Alias: j follows the coordinator's logs"
alias_line="alias j='journalctl -u aa-coordinator -f'"
touch "$HOME/.bash_aliases"
grep -qxF "$alias_line" "$HOME/.bash_aliases" || echo "$alias_line" >> "$HOME/.bash_aliases"

cat <<DONE

Done. Still to do by hand:
  1. Copy each sheet's config and service account into $REPO/secrets/
  2. Open port 5432 to the workers, if anything between them blocks it
  3. Register the sheets, which start in shadow mode:
       poetry run python -m auto_archiver.job_queue.sync register secrets/<config>.yaml
  4. Start it, and watch it:
       sudo systemctl start aa-coordinator
       j     (journalctl -u aa-coordinator -f, from your next login)

Workers connect with: postgresql://$DB_USER:<password>@<this VM's IP>:5432/$DB_NAME
DONE
