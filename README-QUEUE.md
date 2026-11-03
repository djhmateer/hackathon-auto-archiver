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
3. Register the sheets. They start off, so sync doesn't read them until you move them to the queue:

   ```bash
   poetry run python -m auto_archiver.job_queue.sync register secrets/<config>.yaml
   ```

4. Start it, and watch the logs:

   ```bash
   sudo systemctl start aa-coordinator
   j                                       # journalctl -u aa-coordinator -f, from your next login
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
   j                                       # journalctl -u aa-worker-supervisor -f
   ```

   Look for `Started worker <worker_name>, … daily reboot at 05:20`, then `Waiting for jobs: …`. The coordinator logs `Worker <worker_name> is up`.

A worker VM runs one job at a time, so more capacity means more VMs. It reboots daily at 05:20 UK time. To change that, edit `/etc/systemd/system/aa-worker-supervisor.service`, then run `sudo systemctl daemon-reload`.

A VM should run cron or a worker supervisor, not both.

Add the new worker's SSH port to `PORTS` at the top of `scripts/job_queue/deploy-workers.sh`, so updates reach it.

## Move a spreadsheet from cron to the queue

1. Register it, if it isn't already. See "Deploy a new coordinator VM".
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

To deploy everything, do the coordinator first, as it applies any new migration, then the workers. The two deploy scripts run from your own machine over SSH, so you need key access to each VM and passwordless `sudo` on it. The host and ports are set at the top of each script.

### Coordinator

Push your changes to `main` first, as the coordinator pulls from GitHub. The script refuses to run if anything is uncommitted, including untracked files, or if a commit isn't pushed yet. Then, from the repo root on your own machine:

```bash
bash scripts/job_queue/deploy-coordinator.sh                         # stop, pull, install, migrate, start
bash scripts/job_queue/deploy-coordinator.sh --reset-except-sheets   # the same, and empty the queue to start again
```

It stops at the first step that fails, which leaves the coordinator stopped.

After migrating, `--reset-except-sheets` empties `job_files`, `sheet_writes`, `jobs`, `drive_folders`, `admin_alerts` and `heartbeats`, and restarts their ids from 1. It keeps `sheets`, which includes whether each sheet is on, and `schema_migrations`. Before you use it:

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

Deploy the coordinator first if there's a new migration. Then, from the repo root on your own machine:

```bash
bash scripts/job_queue/deploy-workers.sh
```

It makes the same commit and push checks, then does each worker in turn, so only one is down at a time. It doesn't wait for running jobs: it kills them, and each is retried once its lease expires, within 5 minutes. The workers' SSH ports are set at the top of the script.

To do it by hand instead, on each worker VM, finish the running jobs first. This returns once the signal is sent, so wait for the service to stop:

```bash
sudo systemctl kill --kill-who=main -s SIGTERM aa-worker-supervisor
systemctl status aa-worker-supervisor      # wait until it says "inactive (dead)" before going on
git pull origin main
poetry sync --only main                    # not install: also removes packages that aren't in main
sudo systemctl start aa-worker-supervisor
j                                          # journalctl -u aa-worker-supervisor -f
```

## Stop a worker

| Command | Running jobs |
|---|---|
| `sudo systemctl kill --kill-who=main -s SIGTERM aa-worker-supervisor` | Finish, however long they take |
| `sudo systemctl stop aa-worker-supervisor` | Killed after 15 minutes, then retried once the lease expires |
| Ctrl-C, when running in a terminal | Finish, however long they take |

## Archive stats

Each job a worker finishes saves its stats, for a dashboard. Jobs finished before migration 003, or by a worker on older code, have none. Times are UTC.

- `jobs`, besides the sheet, row, folder, worker and timings it already had: `title`, `archive_url`, `extractor` (eg `vimeo`), `posted_at` (when the original was posted), `cached`, `store_failures` (files that didn't upload) and the worker's `git_commit`. The archive took `finished_at - work_started_at`, and waited `started_at - added_at` in the queue.
- `job_files`: one row per stored file for each attempt, so a retry's files count too. Each has its `role` (eg `browsertrix`, `metadata_json`, blank for the main media), `key`, `url`, `mimetype` (`application/wacz` for a WACZ), `bytes`, `hash` and `duration_seconds`. Every file in an archive is hashed with the hash enricher's algorithm, eg `SHA3-512:…`, including files it didn't cover, such as screenshots and the WACZ. `posted_at` and `duration_seconds` are empty when the extractor didn't find them.
- `sheets.client`: who the sheet is for. Set it by hand.

If the stats can't be worked out, the result is saved without them, and the archiver logs `Couldn't work out the archive's stats`.

```bash
export PGPASSWORD=password
Q="psql -h localhost -U bob -d auto_archiver -c"

$Q "UPDATE sheets SET client = '<client_name>' WHERE spreadsheet_id = '<spreadsheet_id>'"

# the last 20 archives: files, size and how long each took
$Q "SELECT j.id, j.row_number, j.folder, j.extractor, count(f.id) AS files,
           pg_size_pretty(sum(f.bytes)) AS size, j.finished_at - j.work_started_at AS took
    FROM jobs j LEFT JOIN job_files f ON f.job_id = j.id
    WHERE j.status = 'done' GROUP BY j.id ORDER BY j.id DESC LIMIT 20"

# storage by file type
$Q "SELECT mimetype, count(*), pg_size_pretty(sum(bytes)) FROM job_files GROUP BY mimetype ORDER BY sum(bytes) DESC"

# storage by client
$Q "SELECT s.client, count(DISTINCT j.id) AS archives, pg_size_pretty(sum(f.bytes)) AS size
    FROM job_files f JOIN jobs j ON j.id = f.job_id JOIN sheets s ON s.id = j.sheet_id GROUP BY s.client"
```

## Add a migration

Add the next numbered file to `src/auto_archiver/job_queue/migrations/`, such as `007_add_something.sql`, and run `migrate`. Never edit a file that's already been applied.

The coordinator won't start if a migration isn't applied, an applied file has been edited, or the database has a migration its code doesn't.

## Check on it

```bash
export PGPASSWORD=password
Q="psql -h localhost -U bob -d auto_archiver -c"

$Q "SELECT status, count(*) FROM jobs GROUP BY status"
$Q "SELECT id, row_number, status, attempts, worker, error FROM jobs ORDER BY id DESC LIMIT 20"
$Q "SELECT name, beat_at FROM heartbeats"
$Q "SELECT kind, message, created_at FROM admin_alerts ORDER BY id DESC LIMIT 20"
```

Alerts aren't emailed yet, so check them by hand. A sheet write that hit a conflict waits for you:

```bash
poetry run python -m auto_archiver.job_queue.writer conflicts
poetry run python -m auto_archiver.job_queue.writer retry <id>
poetry run python -m auto_archiver.job_queue.writer discard <id>
```

Logs:

- **Coordinator and worker supervisor:** `journalctl -u aa-coordinator -f` and `journalctl -u aa-worker-supervisor -f`. The install scripts add a `j` alias for this to `~/.bash_aliases` on each VM.
- **A job that isn't being claimed:** only one job per site (eg vimeo.com) runs at a time. The coordinator logs `Not claimed yet: job <id> … waits for its site <domain>, as job <id> on worker <worker_name> is running it` once for each job held back like this.
- **Archiver:** `logs/`, such as `1debug.log` and `hangs.log`
- **Errors during a job's setup:** `logs/last-job-console.log`
