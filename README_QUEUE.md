# Job queue: operations guide

How to install, run, deploy and operate the Postgres job queue. The design, and the reasoning behind it, are in [queue-spec.md](queue-spec.md).

## Contents

- [Overview](#overview)
- [Configuration](#configuration)
- [Running locally](#running-locally)
- [Installing a coordinator VM](#installing-a-coordinator-vm)
- [Installing a worker VM](#installing-a-worker-vm)
- [Deploying updates](#deploying-updates)
- [Updating the OS](#updating-the-os)
- [Moving a spreadsheet from cron to the queue](#moving-a-spreadsheet-from-cron-to-the-queue)
- [Stopping a worker](#stopping-a-worker)
- [Monitoring](#monitoring)
- [Troubleshooting](#troubleshooting)
- [Archive stats](#archive-stats)
- [Backups](#backups)
- [Schema migrations](#schema-migrations)
- [Reference: timings and limits](#reference-timings-and-limits)

## Overview

| Component | Where it runs | What it does |
|---|---|---|
| Postgres | Coordinator VM | Holds the queue: `sheets`, `jobs`, `sheet_writes`, `job_files`, `admin_alerts`, `heartbeats`, `drive_folders` |
| Coordinator (`aa-coordinator`) | Coordinator VM, one only | Runs four steps in a loop on one thread. **Sync** reads the sheets and queues rows. The **sheet writer** writes statuses and results back to the sheets. **Cleanup** requeues jobs whose worker has died. **Alerts** emails the alerts that need acting on. |
| Worker supervisor (`aa-worker-supervisor`) | Each worker VM | Claims one job at a time and runs the archiver for it as a child process. It renews the job's lease and enforces the time limit. |

A row moves through the queue like this:

1. Sync finds a row with a URL and a blank status, adds a job and sets the row to `Queued`.
2. A worker claims the job, and the row shows `Archive in progress (<worker_name>)`.
3. The archiver saves its result to Postgres. The sheet writer copies it to the row.

Only the sheet writer writes to the sheets. It sends only each job's latest status, and the archiver owns the status cell, so it overwrites whatever is there. Before each write it re-reads the row's URL. If the URL has changed, the rows have moved, so it drops that job's writes and raises a `writer_discarded` alert instead of writing to the wrong row. The result stays in Postgres.

Run every command from the repo root.

## Configuration

The database connection string is read from a `.env` file in the repo root, which git ignores:

```
AA_DATABASE_URL=postgresql://<user>:<password>@<host>:5432/auto_archiver
```

On the coordinator, the same file also holds the Postmark settings for alert emails. Put them in your own `.env`, and `deploy-coordinator.sh` copies them across. Until they're set, the coordinator logs a warning every 5 minutes while an alert is waiting to be emailed:

```
POSTMARK_API_TOKEN=<server token>
POSTMARK_FROM_EMAIL=<an address with a Postmark sender signature>
ALERT_EMAIL_TO=<where alerts go>
```

The install scripts create this file. Sheet configs (`secrets/orchestration-*.yaml`), service accounts, cookies and sessions are not in git and must be copied to each VM.

## Running locally

```bash
poetry run python -m auto_archiver.job_queue.migrate              # apply any pending migrations
poetry run python -m auto_archiver.job_queue.coordinator          # terminal 1
poetry run python -m auto_archiver.job_queue.worker_supervisor    # terminal 2
```

The supervisor accepts `--worker <name>` (the default is the hostname) and `--reboot-at HH:MM` (UK time; if it's left out, the worker never reboots itself).

The coordinator's steps can also be run on their own, for debugging:

```bash
poetry run python -m auto_archiver.job_queue.sync run --once
poetry run python -m auto_archiver.job_queue.writer run --once
```

## Installing a coordinator VM

Target: Ubuntu 26.04. The script also installs Postgres 18.

```bash
git clone https://github.com/djhmateer/hackathon-auto-archiver.git ~/auto-archiver
cd ~/auto-archiver
bash scripts/job_queue/install-coordinator.sh
```

Run it as your normal user, not root. It is safe to run again. It installs Postgres, creates the database, writes `.env`, applies the migrations and installs the `aa-coordinator` service. It doesn't start the service.

Then:

1. Copy each sheet's config and service account into `secrets/`.
2. If a firewall sits between the coordinator and the workers, open port 5432 to the workers.
3. Register each sheet. Its tabs start **off**, so sync doesn't read them until you enable them (see [Moving a spreadsheet from cron to the queue](#moving-a-spreadsheet-from-cron-to-the-queue)):

   ```bash
   poetry run python -m auto_archiver.job_queue.sync register secrets/<config>.yaml
   ```

4. Start the service and watch its log:

   ```bash
   sudo systemctl start aa-coordinator
   j                                   # alias for journalctl -u aa-coordinator -f, from your next login
   ```

Postgres accepts remote connections only from subnets the VM is directly attached to (`samenet` in `pg_hba.conf`). A worker on another subnet needs its own `pg_hba.conf` line.

## Installing a worker VM

Target: Ubuntu 26.04.

```bash
git clone https://github.com/djhmateer/hackathon-auto-archiver.git ~/auto-archiver
cd ~/auto-archiver
bash scripts/job_queue/install-worker.sh --coordinator-database <coordinator_ip>
```

Run it as your normal user, not root. It is safe to run again. It installs:

- Firefox, geckodriver and Chrome
- Docker, with the browsertrix-crawler and YouTube token containers
- Deno, ffmpeg, exiftool and tesseract
- Python 3.12 and the project

It then checks that it can reach Postgres. If that check fails and the worker is on a different subnet from the coordinator, add a `pg_hba.conf` line for it on the coordinator.

The script enables the `aa-worker-supervisor` service but doesn't start it. Then, in this order:

1. **Copy the secrets.** Copy into `secrets/` every config the queue runs, with its service accounts, cookies, sessions and browser profiles, at the same paths as on the coordinator. Do this before rebooting. The service starts by itself on boot, and without the configs every job fails in setup.
2. **Reboot** with `sudo reboot`. This applies the `docker` group membership and frees the 512 MB of memory that was reserved for crash dumps (the script removes `kdump-tools`).
3. **Check the log** with `j`, an alias for `journalctl -u aa-worker-supervisor -f`. Look for `Started worker <worker_name>, … daily reboot at 05:20`, followed by `Waiting for jobs: …`. The coordinator logs `Worker <worker_name> is up`.
4. **Add the worker to the deploy scripts.** Add its SSH port to `PORTS` in `scripts/job_queue/deploy-workers.sh`, so that deploys reach it. Also add it to the `for CONFIG_PORT in …` loop in both deploy scripts, so that it receives the sheet configs.

Notes:

- A worker runs one job at a time. To add capacity, add VMs.
- Workers reboot daily at 05:20 UK time. If a job is still running at that point, the reboot waits for it, for up to an hour. To change the time, edit `/etc/systemd/system/aa-worker-supervisor.service` and run `sudo systemctl daemon-reload`.
- A VM should run either the old cron archiver or a worker supervisor, never both.

## Deploying updates

Deploy the coordinator first, because it applies any new migrations, and then the workers. Both scripts run from your own machine over SSH, and need key-based access and passwordless `sudo` on each VM. The host, SSH ports and remote repo path are set at the top of each script.

Before deploying, both scripts:

- refuse to run if there are uncommitted changes (including untracked files) or commits that haven't been pushed to `origin/main`, because the VMs pull from GitHub;
- copy the sheet configs listed in `CONFIGS` from your local `secrets/` to the coordinator and every worker. The coordinator writes cells using its copy's column names, and the workers archive with theirs, so a stale copy silently drops new columns. When you add a sheet, add its config to `CONFIGS` in **both** scripts.

Service accounts, cookies and sessions are not copied automatically. If you deploy by hand, the configs aren't copied either, so copy any that have changed.

Both scripts install Python packages exactly as pinned in the committed `poetry.lock`. To update packages, run `poetry update` on your own machine, run the tests, then commit and push the lock file. Never run `poetry update` on a VM: it changes `poetry.lock` there, and the next deploy's `git pull` then fails.

### Coordinator

```bash
bash scripts/job_queue/deploy-coordinator.sh                         # stop, pull, sync packages, back up, migrate, start
bash scripts/job_queue/deploy-coordinator.sh --reset-except-sheets   # the same, then empty the queue
```

Before stopping the coordinator, it also copies the three alert email settings from your `.env` into the coordinator's `.env`, replacing any older copies. It keeps the coordinator's own `AA_DATABASE_URL`, and stops if any of the three is missing from your `.env`. It never prints their values.

It backs up the database before migrating, and adds the nightly backup to the coordinator's crontab if it isn't there. See [Backups](#backups).

The script stops at the first step that fails, which leaves the coordinator stopped.

`--reset-except-sheets` runs after the migrations. It empties `job_files`, `sheet_writes`, `jobs`, `drive_folders`, `admin_alerts` and `heartbeats` and restarts their ids from 1. It keeps `sheets` (including whether each tab is enabled) and `schema_migrations`. Before you use it:

- Stop the workers first. A job that is running during the reset can't save its result, because its row no longer exists.
- After the reset, the next sync queues every row with a blank status again.
- Paused sheets stay paused, because `setup_failures` and `paused_until` are kept.

To deploy by hand, on the coordinator VM:

```bash
git pull origin main
poetry sync --only main                    # sync, not install, so removed packages are uninstalled
poetry run python -m auto_archiver.job_queue.migrate
sudo systemctl restart aa-coordinator
```

### Workers

```bash
bash scripts/job_queue/deploy-workers.sh
```

The script runs the same checks, then updates one worker at a time, so only one is down at once. **It kills running jobs rather than waiting for them.** Each killed job is retried within about 9 minutes: the lease expires after 5, cleanup requeues it 2 minutes after that, and the job becomes claimable 2 minutes later.

On each worker, after syncing the packages, it also updates the Docker images:

- pulls `webrecorder/browsertrix-crawler:latest`, which the code runs untagged;
- pulls the `brainicism/bgutil-ytdlp-pot-provider` image whose tag matches the installed `bgutil-ytdlp-pot-provider` plugin (from `poetry.lock`), because the YouTube token server and the plugin must be the same version;
- runs `docker image prune -f`, which removes only untagged images, such as the browsertrix image a pull replaced (3.6 GB each);
- recreates the `bgutil-provider` container on that image, checks that the container is running it, and prints its `/ping` reply, which includes the version. A few `curl: (52)` or `(56)` lines before the reply are normal while the server starts.

To deploy by hand without killing the running job, on each worker VM:

```bash
sudo systemctl kill --kill-who=main -s SIGTERM aa-worker-supervisor   # finish the running job, then stop
systemctl status aa-worker-supervisor      # wait for "inactive (dead)"
git pull origin main
poetry sync --only main                    # sync, not install, so removed packages are uninstalled
sudo systemctl start aa-worker-supervisor
```

The Docker images aren't updated when you deploy by hand. To update them, follow the steps in `scripts/job_queue/deploy-workers.sh`.

## Updating the OS

```bash
bash scripts/job_queue/update-os.sh
```

This upgrades the Ubuntu packages on the coordinator, then on each worker in turn, and reboots each VM. The upgrade includes Firefox, Chrome and Docker, which come from their own apt repos. Ubuntu's automatic updates don't cover those repos. It is kept separate from the deploys, so that a problem with new code and a problem with an OS update can be told apart. Run it every few weeks, when the queue is quiet.

For each VM, the script:

1. stops the service. On a worker, it first kills the running job, as `deploy-workers.sh` does;
2. runs `apt-get update` and `apt-get upgrade` without prompting. Edited config files are kept, and new dependencies such as a new kernel are installed, but nothing is removed;
3. reboots, then waits up to 5 minutes for the VM to come back. It checks that the boot id has changed, so that a VM that hasn't gone down yet isn't mistaken for one that's back;
4. waits for the service to start on its own, then prints the kernel version and the last log lines. On a worker it also prints the Firefox, Chrome, Docker and `bgutil-provider` versions; on the coordinator, the Postgres version.

The coordinator goes first. While it reboots, no jobs are claimed, and running jobs keep retrying their database writes for about 5 minutes, which is longer than a reboot takes. The script stops at the first step that fails, before it moves on to the next VM. It needs passwordless `sudo` for `apt-get`, as the deploys do for `systemctl`. It doesn't run `apt autoremove`, so old kernels build up in `/boot` over time.

## Moving a spreadsheet from cron to the queue

1. Register the spreadsheet if you haven't already (see [Installing a coordinator VM](#installing-a-coordinator-vm)).
2. Turn off the spreadsheet's cron line, and wait for the current cron run to finish.
3. Check that no row still says `Archive in progress`. Blank such a row to rerun it, or set it to a final status.
4. Enable it. All its tabs move to the queue, and any tabs added later start enabled:

   ```bash
   poetry run python -m auto_archiver.job_queue.sync enable <spreadsheet_id>
   ```

   The id is in `sheets.spreadsheet_id`.

To move a spreadsheet back to cron, leave the coordinator running and repeat the command below until it reports `Cron can go back on`. Only then re-enable the cron line:

```bash
poetry run python -m auto_archiver.job_queue.sync rollback <spreadsheet_id>
```

Each run turns the spreadsheet's tabs off and cancels any queued jobs, which clears their status. It then reports how many jobs are still running and how many sheet writes are still to be sent. Cron is only safe once both are zero.

## Stopping a worker

| Command | Effect on the running job |
|---|---|
| `sudo systemctl kill --kill-who=main -s SIGTERM aa-worker-supervisor` | Lets it finish, however long it takes, then stops |
| `sudo systemctl stop aa-worker-supervisor` | Lets it run for up to 15 minutes, then kills it. It is retried once its lease expires. |
| Ctrl-C (when running in a terminal) | Lets it finish, however long it takes, then stops |

## Monitoring

On the coordinator, `d` (or `poetry run python -m auto_archiver.job_queue.dashboard` from the repo) shows a dashboard that refreshes every 5 seconds until Ctrl+C. It only reads the database. It shows the coordinator's heartbeat, queue totals and the last backup; each worker's heartbeat, running job and today's done and failed counts; each tab's queued, running, and last 24 hours' done and failed, with its rows, blank statuses and when sync last read it (red if a tab that's on hasn't been read for a minute); queued jobs that can't be claimed yet and why; the last 10 finished jobs with their sheet status; and unsent and recent alerts. Green is fine, yellow is worth a look, red needs action.

For anything else, query the database:

```bash
export PGPASSWORD=<password>
Q="psql -h localhost -U <user> -d auto_archiver -c"

$Q "SELECT status, count(*) FROM jobs GROUP BY status"
$Q "SELECT id, row_number, status, attempts, worker, error FROM jobs ORDER BY id DESC LIMIT 20"
$Q "SELECT name, beat_at FROM heartbeats"
$Q "SELECT kind, message, created_at FROM admin_alerts ORDER BY id DESC LIMIT 20"
```

These alerts are emailed (see [Configuration](#configuration)): `site_blocked`, `sheet_paused`, `worker_paused`, `writer_discarded` and `reboot_failed`. They're listed in `EMAIL_KINDS` in `job_queue/alerts.py`. `sheet_disagrees` is raised once a day per row for as long as the disagreement lasts, so it isn't emailed. Check `admin_alerts` for it now and then. Each email ends with the alert's kind; [Troubleshooting](#troubleshooting) says what to do for each one.

A `site_blocked` alert means a site in `EMAIL_WHEN_BLOCKED` (in `queue_feeder_db.py`, currently `idf.il`) blocked the crawler, and the status asks for a requeue (`... (blocked by site - please requeue)`). These blocks usually lift within hours, so clear the row's status to archive it again. The coordinator emails each one once, with the sheet, row, URL and status, then sets its `sent_at`. If Postmark fails, it tries again 5 minutes later.

A `writer_discarded` alert means a job's result never reached the sheet, because its row's URL changed or its tab was deleted. The result is still in the `jobs` table. If the URL moved to another row, that row shows `Archive in progress` with no job and raises a `sheet_disagrees` alert. Clear its status to archive it again.

### Logs

| What | Where |
|---|---|
| Coordinator | `journalctl -u aa-coordinator -f` (alias `j` on the coordinator VM) |
| Worker supervisor | `journalctl -u aa-worker-supervisor -f` (alias `j` on each worker VM) |
| Archiver | `logs/` on the worker, e.g. `1debug.log`, and `hangs.log` for stack dumps of stalled rows |
| Errors during a job's setup, and crashes | `logs/last-job-console.log` on the worker (last job only) |

The coordinator's log is the best single view of the system. It records each job as it is queued, claimed and finished, each worker coming up or going quiet, and every sheet write.

## Troubleshooting

**A queued job isn't being claimed.** Only one job per site runs at a time. The coordinator logs each job held back for this reason once: `Not claimed yet: job <id> … waits for its site <domain>, as job <id> on worker <worker_name> is running it`. A job can also be waiting for a retry delay, or belong to a sheet that is paused or not enabled.

**A sheet has been paused.** After 3 setup failures in a row (the archiver exits before it starts work), the sheet is paused for 15 minutes and an alert is raised. The cause is usually a missing or broken config, or missing secrets on a worker. Check `logs/last-job-console.log` on the worker. The pause clears once a job from that sheet gets past setup.

**A worker has stopped claiming jobs.** If a worker has 3 quick failures in a row across different sheets, it stops claiming for 15 minutes and raises an alert, so that a broken VM can't empty the queue.

**A Vimeo job produced no video.** If a `vimeo.com` job finishes as `wacz: success`, the coordinator logs a warning: `Job <id> got no video from vimeo.com, only a WACZ of the page`. The sheet shows success, but Cloudflare is probably challenging the workers' IP address. Open a Vimeo video in a browser on the same network, pass the "Verify you are human" check, then clear the row's status so it runs again.

**A `writer_discarded` alert.** A job's result never reached the sheet, because its row's URL changed or its tab was deleted. See [Monitoring](#monitoring).

**A `reboot_failed` alert.** The worker couldn't run `sudo -n /sbin/reboot`. It carries on archiving and doesn't try again until the supervisor restarts. Check `/etc/sudoers.d/aa-reboot` on the worker (`install-worker.sh` writes it), then restart the supervisor or reboot the VM by hand.

**A row says `Queued` or `Archive in progress` but has no job.** Sync raises a `sheet_disagrees` alert, at most once a day per row. Usually the row was edited by hand, or the queue was reset. Clear the status to rerun the row.

## Archive stats

Each job a worker finishes records statistics for reporting. Times are in UTC. Jobs finished before migration 003, or by a worker on older code, have no stats.

- **`jobs`**: as well as the sheet, row, folder, worker and timings, each job records:
  - `title` and `archive_url`
  - `extractor`, the one that succeeded (e.g. `vimeo`)
  - `posted_at`, when the original was posted
  - `cached`
  - `store_failures`, the number of files that failed to upload
  - `git_commit`, the worker's code version

  The archive took `finished_at - work_started_at`, and the job waited `started_at - added_at` in the queue.
- **`job_files`**: one row per stored file per attempt, so a retry's files are counted too. Each row has:
  - `role` (e.g. `browsertrix` or `metadata_json`; blank for the main media)
  - `key`, `url`, `mimetype` (`application/wacz` for a WACZ) and `bytes`
  - `hash`. Every file is hashed with the hash enricher's algorithm (e.g. `SHA3-512:…`), including files the enricher didn't cover, such as screenshots and WACZs.
  - `duration_seconds`

  `posted_at` and `duration_seconds` are empty when the extractor didn't find them.
- **`sheets.client`**: who the sheet is for. Set it by hand.

If the stats can't be worked out, the result is saved without them, and the archiver logs `Couldn't work out the archive's stats`.

```bash
$Q "UPDATE sheets SET client = '<client_name>' WHERE spreadsheet_id = '<spreadsheet_id>'"

# the last 20 archives: number of files, total size and time taken
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

## Backups

`scripts/job_queue/backup-db.sh` dumps the queue database on the coordinator to `~/backups/auto_archiver_<weekday>.dump` (e.g. `auto_archiver_Mon.dump`), so there are always the last 7 days. It runs nightly at 02:30 UTC from cron, and before each deploy migrates. Each run appends a line to `~/backups/backup.log`. It reads `AA_DATABASE_URL` from the repo's `.env`.

The backups are on the coordinator VM itself, so they cover mistakes, a bad migration or a reset, but not losing the VM.

To back up by hand, on the coordinator: `bash scripts/job_queue/backup-db.sh`

To restore one over the current database, stop the coordinator and the workers' supervisors first, then on the coordinator:

```bash
sudo systemctl stop aa-coordinator
pg_restore --clean --if-exists --no-owner -d "<AA_DATABASE_URL>" ~/backups/auto_archiver_<weekday>.dump
sudo systemctl start aa-coordinator
```

## Schema migrations

Migrations are plain SQL files in `src/auto_archiver/job_queue/migrations/`, applied in filename order, each in its own transaction. To change the schema, add the next numbered file (e.g. `007_add_something.sql`) and run:

```bash
poetry run python -m auto_archiver.job_queue.migrate
```

Never edit a file that has already been applied: its checksum is recorded. The coordinator refuses to start in any of these cases:

- a migration is pending;
- an applied file has been edited;
- the database has a migration that the code doesn't.

## Reference: timings and limits

| Setting | Value | Defined in |
|---|---|---|
| Coordinator cycle (writer, cleanup) | 5 s | `coordinator.py` `CYCLE_SECONDS` |
| Sheet sync interval | 15 s | `coordinator.py` `SYNC_SECONDS` |
| Worker tick and heartbeat | 2 s | `worker_supervisor.py` `TICK_SECONDS` |
| Worker reported as quiet after | 30 s without a heartbeat | `coordinator.py` `WORKER_QUIET_SECONDS` |
| Lease length, and how often it's renewed | 5 min, renewed every 60 s | `db.py` `claim_job`, `worker_supervisor.py` `RENEW_SECONDS` |
| Requeue after a lease expires | 2 min grace, then claimable 2 min later | `db.py` `requeue_expired` |
| Attempts before a job fails | 3 | `db.py` `MAX_ATTEMPTS` |
| Setup failures before a sheet is paused | 3, then paused for 15 min | `db.py` `MAX_SETUP_FAILURES` |
| Job time limit | 3 h in the archiver, with a 3 h 5 min backstop in the supervisor | `worker_supervisor.py` `TIME_LIMIT_SECONDS` |
| Daily reboot | 05:20 UK time, forced after 1 h if a job is still running | service file `--reboot-at`, `FORCE_REBOOT_AFTER_SECONDS` |
