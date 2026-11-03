# Job queue

How to run and deploy the Postgres job queue. For the design, see `queue-spec.md`.

- **Coordinator**: one, on the Postgres VM. Syncs the sheets, writes results back and requeues jobs whose worker died.
- **Worker supervisor**: one per worker VM. Claims jobs and runs the archiver for each one.

Run every command from the repo root. The database connection comes from a `.env` file in the repo root, which git ignores:

```
AA_DATABASE_URL=postgresql://bob:password@localhost:5432/auto_archiver
```

## Run locally

```bash
# checks and applies migrations (connection string read from .env)
poetry run python -m auto_archiver.job_queue.migrate
# run the coordinator
poetry run python -m auto_archiver.job_queue.coordinator
# run the worker
poetry run python -m auto_archiver.job_queue.worker_supervisor
```

Run the last two in separate terminals.

## Deploy a new coordinator VM

Ubuntu 26.04. This also installs Postgres 18 on the VM.

```bash
git clone <repo_url> /home/dave/auto-archiver
cd /home/dave/auto-archiver
git pull origin main
bash scripts/job_queue/install-coordinator.sh
```

Run the script as your normal user, not root. It's safe to run again. It doesn't start the coordinator, so then:

1. Copy each sheet's config and service account into `secrets/` with FileZilla.
2. Open port 5432 to the workers, if anything between them blocks it.
3. Register the sheets, which start in shadow mode, so they're only read:

   ```bash
   poetry run python -m auto_archiver.job_queue.sync register secrets/<config>.yaml
   ```

4. Start it, and watch the logs:

   ```bash
   sudo systemctl start aa-coordinator
   journalctl -u aa-coordinator -f
   ```

Postgres only accepts connections from subnets the VM is directly attached to. A worker elsewhere needs its own `pg_hba` line.

## Deploy a new worker VM

Ubuntu 26.04. Before you start:

```bash
git clone https://github.com/djhmateer/hackathon-auto-archiver.git /home/dave/auto-archiver
cd /home/dave/auto-archiver
bash scripts/job_queue/install-worker.sh --coordinator-database 172.16.45.40
```

Run the script as your normal user, not root. It's safe to run again. It installs Firefox, geckodriver, Chrome, Docker, the browsertrix-crawler and YouTube token containers, Deno, ffmpeg, exiftool, tesseract, Python 3.12 and the project, then checks it can use Postgres. If that check fails and the VM is on another subnet from the coordinator, the coordinator needs a `pg_hba` line for it.

The script enables the service but doesn't start it, so then, in this order:

1. Copy into `secrets/` with FileZilla every config the queue runs, with its service accounts, cookies, sessions and profiles, at the same paths as on the coordinator. Do this before rebooting: the service starts by itself on the reboot, and without the configs every job fails in setup, and the worker pauses after 3 quick failures.
2. Reboot, so the `docker` group applies: `sudo reboot`. The worker starts by itself.
3. Watch the logs:

   ```bash
   journalctl -u aa-worker-supervisor -f
   ```

   Look for `Started worker <worker_name>, … daily reboot at 05:20`, then `Waiting for jobs: …`. The coordinator logs `Worker <worker_name> is up`.

A worker VM runs one job at a time, so more capacity means more VMs. It reboots daily at 05:20 UK time. To change that, edit `/etc/systemd/system/aa-worker-supervisor.service`, then run `sudo systemctl daemon-reload`.

A VM should run cron or a worker supervisor, not both.

## Move a spreadsheet from cron to the queue

1. Check the shadow mode row counts look right in the coordinator's logs.
2. Turn off the spreadsheet's cron line, and wait for the cron run to finish.
3. Check no row still says `Archive in progress`. Blank it for a rerun, or set it to a final status.
4. Turn it on. All its tabs move, and any tabs added later start on:

   ```bash
   poetry run python -m auto_archiver.job_queue.sync enable <spreadsheet_id>
   ```

The id is in `sheets.spreadsheet_id`.

To move it back, keep the coordinator running and run this until it says `Cron can go back on`. Only then turn the cron line back on:

```bash
poetry run python -m auto_archiver.job_queue.sync rollback <spreadsheet_id>
```

## Update the code

### Coordinator

Push your changes to `main` first, as the coordinator pulls from GitHub. The script refuses to run if anything is uncommitted, including untracked files, or if a commit isn't pushed yet. Then, from the repo root on your own machine:

```bash
bash scripts/job_queue/deploy-coordinator.sh
```

This connects to the coordinator over SSH at `192.168.1.212` on port `340`, so you need key access to it. The host and port are set at the top of the script. On the coordinator it then:

1. stops `aa-coordinator`
2. pulls `main`
3. runs `poetry install --only main`
4. runs the migrations
5. starts `aa-coordinator`, and shows whether it's running and the last 20 log lines

It stops at the first step that fails. If that happens, the coordinator stays stopped until you fix the problem and run the script again.

To start again with an empty queue, add `--reset-except-sheets`:

```bash
bash scripts/job_queue/deploy-coordinator.sh --reset-except-sheets
```

After migrating, this empties `outbox`, `jobs`, `shadow_jobs`, `drive_folders`, `alerts` and `heartbeats`, and restarts their ids from 1. It keeps `sheets`, which includes each sheet's enabled/shadow setting, and `schema_migrations`. Before you use it:

- Stop the workers first. A job that's running when you reset can't write back its result once its row has gone.
- The next sync queues again every row whose status is blank.
- A sheet that's paused stays paused, as its `setup_failures` and `paused_until` are kept.

To do it by hand instead, on the coordinator VM:

```bash
git pull origin main
poetry install --only main
poetry run python -m auto_archiver.job_queue.migrate
sudo systemctl restart aa-coordinator
j                                          # journalctl -u aa-coordinator -f
```

### Workers

On each worker VM, finish the running jobs first. This returns once the signal is sent, so wait for the service to stop:

```bash
sudo systemctl kill --kill-who=main -s SIGTERM aa-worker-supervisor
systemctl status aa-worker-supervisor      # wait until it says "inactive (dead)" before going on
git pull origin main
poetry sync --only main                    # not install: also removes packages that aren't in main
sudo systemctl start aa-worker-supervisor
journalctl -u aa-worker-supervisor -f
```

## Stop a worker

| Command | Running jobs |
|---|---|
| `sudo systemctl kill --kill-who=main -s SIGTERM aa-worker-supervisor` | Finish, however long they take |
| `sudo systemctl stop aa-worker-supervisor` | Killed after 15 minutes, then retried once the lease expires |
| Ctrl-C, when running in a terminal | Finish, however long they take |

## Add a migration

Add the next numbered file to `src/auto_archiver/job_queue/migrations/`, such as `002_add_something.sql`, and run `migrate`. Never edit a file that's already been applied.

The coordinator won't start if a migration isn't applied, an applied file has been edited, or the database has a migration its code doesn't.

## Check on it

```bash
export PGPASSWORD=password
Q="psql -h localhost -U bob -d auto_archiver -c"

$Q "SELECT status, count(*) FROM jobs GROUP BY status"
$Q "SELECT id, row_number, status, attempts, worker, error FROM jobs ORDER BY id DESC LIMIT 20"
$Q "SELECT name, beat_at FROM heartbeats"
$Q "SELECT kind, message, created_at FROM alerts ORDER BY id DESC LIMIT 20"
```

Alerts aren't emailed yet, so check them by hand. A sheet write that hit a conflict waits for you:

```bash
poetry run python -m auto_archiver.job_queue.writer conflicts
poetry run python -m auto_archiver.job_queue.writer retry <id>
poetry run python -m auto_archiver.job_queue.writer discard <id>
```

Logs:

- **Coordinator and worker supervisor:** `journalctl -u aa-coordinator -f` and `journalctl -u aa-worker-supervisor -f`. The install scripts add a `j` alias for this to `~/.bash_aliases` on each VM.
- **Archiver:** `logs/`, such as `1debug.log` and `hangs.log`
- **Errors during a job's setup:** `logs/last-job-console.log`
