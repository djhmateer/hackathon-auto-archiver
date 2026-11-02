# Spec: Postgres queue for the archiver

Written 1 Nov 2026. Holds the thinking so far. The queue build is the priority from today. The
hackathon Must items are still open in `/home/dave/code/hackathon/spec.md`.

## Goal

Automatic load balancing, more throughput and faster results for clients. Volume may double or
triple soon, and the design should allow for more later. A big refactor is acceptable.

Build phase 1 first: one shared queue, with each row still running as one job.

## Evidence

From `aa_logs`, 1 Sep to 31 Oct 2026, 14,122 rows, timed from STARTED to DONE.

### Capacity

The workers spend 16% of their time archiving (19 of 120 VM-hours a day).

| Load | Rows per day | Average use across 5 VMs | Busiest pinned VM, worst week |
|---|---|---|---|
| Today | 234 | 16% | 62% |
| 2x | 468 | 32% | 124% |
| 3x | 702 | 47% | 186% |

- The limit at 100% use is about 1,480 rows a day. This assumes 5 rows can run all day without
  slowing each other, which has never been tested.
- Three sheets produce 72% of rows. No sheet ran on more than one VM, so a burst on a sheet can
  only use one VM.
- Simulated shared queue, replaying copies of real rows from the times they started, so rough:

| Load | Median wait | 95th percentile | Worst |
|---|---|---|---|
| 2x | 0 min | 0.5 h | 3 h |
| 3x | 7 min | 3.9 h | 8.1 h |

### Time per row

Average row is about 4 to 5 minutes. Median 197 s, 95th percentile 927 s. The slowest 5% of rows
take 26% of the time. Facebook is 17% of rows and 32% of the time.

Average seconds per row by step, from the last two weeks:

| Group | Steps | Seconds |
|---|---|---|
| Capture | Extractors | about 25 |
| Browser | WACZ 57, screenshot 15, MHTML 12, webdriver 9 | about 94 |
| Waiting on others | Drive 58, OpenTimestamps 31, Wayback 30 | about 119 |

### Rows running at once, all on one host

| Rows | Share of time |
|---|---|
| 0 | 45.9% |
| 1 | 35.0% |
| 2 | 14.7% |
| 3 | 3.8% |
| 4 or 5 | 0.5% |

X, Facebook and Instagram barely slow down with overlap. Telegram and TikTok slow more, possibly
because of disk or because of which sheets were running. Not proven.

### Other findings

- Idle checks start about 10,300 processes a day and use 26 VM-hours. No Sheets quota errors so far.
- Drive `userRateLimitExceeded` warnings rose to 25 to 61 a week since 21 Sep. VM 12 has 186 of 264.
- VM 12 has 33 of the 41 hours lost to log gaps in October, and 18 of the 22 hang warnings.
- 264 rows stuck on "Archive in progress" were relabelled on 28 and 29 Oct, against 40 logged as
  failed. They now read "Problem in archive - run again", which the feeder skips, so they probably
  need a manual rerun.
- The logs do not record when a row was added, so client wait time can't be measured today. The
  queue fixes this with `added_at`.
- Storage logs show about 4,400 uploaded image files and 315 videos a day, including screenshots
  and thumbnails.

## Infrastructure

- `pve2`: 64 GB RAM, software RAID10 on 4 SSDs. Disk IOPS is the bottleneck. All 5 worker VMs are
  here today, allocated 80 GB in total.
- `pve3`: 64 GB DDR5 RAM, single fast SSD (Kingston NV3, 640 TBW if the 2 TB model), same public IP
  as `pve2`, not clustered with it.
- A new Ubuntu 26 VM is being built on `pve3` to replace VM 12. Dave handles the build, the switch
  and the accounts.

Placement for now:

- Development is on Dave's desktop, using its local Postgres.
- Everything new goes on `pve3`, including Postgres and the coordinator, in their own VM with no
  browsers. The existing workers on `pve2` stay on cron.
- `pve3` has a single SSD, so the nightly `pg_dump` must be copied off the host.

Placement later, once the queue is stable:

- `pve2`: Postgres, the coordinator and monitor, because these need the RAID. Moving is a `pg_dump`
  and restore, plus a new `AA_DATABASE_URL`.
- `pve3`: browser-heavy workers, using its SSD as disposable scratch space.
- Glimmer image rating may also run on `pve3` later, at low priority. See
  `/home/dave/code/hackathon/image-spec.md`.
- Everything stays on these two machines. No cloud burst workers.

Postgres:

- Unwritten results exist only in Postgres, so take at least a nightly `pg_dump`.
- `pg_hba` allows `samenet`, the subnets the Postgres VM is attached to, so workers on the same `pve3`
  network can connect. Workers on `pve2` would need their own line.
- The coordinator VM is set up with `scripts/job_queue/install-coordinator.sh`.
- Its VM is left out of the daily reboot.
- The database is called `auto_archiver`. For development it's on the local Postgres:
  `postgresql://bob:password@localhost:5432/auto_archiver`. Code reads the connection string from `.env`
  in the repo root, so production can use its own.
- Production isn't built yet. It will run the latest Postgres 18, the same major version as
  development.
- Tests use a separate database, `auto_archiver_test`, read from `AA_TEST_DATABASE_URL`, and can wipe
  it freely: `postgresql://bob:password@localhost:5432/auto_archiver_test`.

## Decisions

- Postgres is the queue, using `SKIP LOCKED`. No broker. Even 10x volume is well under one task a
  second.
- Python throughout, because workers call the existing Python modules.
- Database access is raw SQL through plain `psycopg` 3, with `libpq5` from `apt` on every host that
  connects. No ORM, connection pool or PgBouncer. Each process opens its own connection.
- Stay on Python 3.12 for now. `pyproject.toml` needs `>=3.10,<3.13`, and an upgrade is its own
  change later, after rollout step 4. The new Ubuntu 26 VM probably has a newer system Python, so
  check, and install 3.12 separately if needed (`uv python install 3.12`, then `poetry env use`).
- Sheets stay as the client interface.
- One job per row in phase 1. Splitting rows into tasks comes later.
- One archiver child process per job, not a long-running worker. It gives clean state, frees memory
  and can be killed with its browsers. Each config loads different modules, and modules share a
  temp folder setting, so one process can't safely run two rows.
- Account and domain limits are not in phase 1. Dave manages accounts. Slots and sheet weights are
  the only concurrency controls. Total slots stay at 5 until the queue is stable.
- No VPN is used, so the orchestrator's VPN code is not a constraint.
- Nothing else reads the sheet status, including client scripts and Uwazi.
- Clients don't sort, insert or delete rows while archiving is pending, but every write still gets a
  sanity check.

## Design

```
Sheets ─► sheet sync ─► Postgres (jobs) ◄─ supervisors ─► one archiver process per job
   ▲                       │
   └── sheet writer ◄── outbox
```

There are two programs to deploy. The coordinator is one process on `pve2` that runs the sheet sync,
sheet writer and cleanup in turn, on one thread with one connection. Each step is wrapped in
`try/except`, Sheets calls have HTTP timeouts so a hang can't block the others, and each cycle updates
a heartbeat row. The supervisor runs on each worker VM.

- **Sheet sync**: reads each enabled sheet every minute or two and adds jobs. It replaces the
  per-sheet polling in the cron scripts. It reads `header`, `columns`, `service_account`, the
  worksheet allow and block lists and the folder settings from each sheet's config. It adds new tabs
  that the config allows, as the feeder does today. The rules are a pure function that takes sheet
  rows and database state and returns actions, so they can be tested with no network.
- **Supervisor**: one per worker VM, with N slots. Each slot claims a job and runs the archiver as a
  child process. It renews the lease every minute and kills the process tree on the time limit. See
  "Supervisor rules".
- **`queue_feeder_db`**: a new module, both Feeder and Database, modelled on `gsheet_feeder_db`. It
  yields the one claimed job and writes results to Postgres, with its own retries, because the
  orchestrator swallows `done()` errors. The job update and outbox entry share a transaction.
  `started()` writes nothing, because the claim has already added the status, but it must keep logging
  `STARTED`, as the timings use it. Archiving no longer touches Sheets, so a Sheets outage can't fail
  a row.
- **Sheet writer**: batches results back to the sheets, reading only the columns it needs. It is the
  only part that writes to them. It sets `written_at` only after the sheet has been updated, which
  sync rule 1 relies on.
- **Cleanup**: requeues jobs whose lease expired more than 2 minutes ago, with a 2-minute delay, and
  fails them after 3 attempts. The grace period lets supervisors renew after a Postgres restart.
- **Alerts**: everything writes to an `alerts` table, read by the daily digest. Urgent ones also go by
  email. This must be in place before the first busy sheet.

The child needs no new orchestrator arguments, because the existing parser handles module settings:

```
auto_archiver --config <config> --feeders queue_feeder_db --databases queue_feeder_db \
    --queue_feeder_db.job_id <id> --queue_feeder_db.lease_token <token>
```

These flags replace the lists in the YAML, so any other databases must be repeated.

The orchestrator barely changes. Nothing outside `gsheet_feeder_db` reads the sheet row context, and
the orchestrator already gets work from a feeder and reports through database hooks. Storage does
read the `folder` context, so sync works out the final folder and stores it in `jobs.folder`.

Split the cell building in `GsheetsFeederDB.done()` in two. The child builds `{column: value}` with
no sheet access. The writer applies `col_exists` and `allow_overwrite_of_spreadsheet_cells` against
the row it has just re-read.

### Supervisor rules

- **Time limit**: `ROW_KILL_SECONDS` stays in the child, as it reports the failure. The supervisor's
  limit is 3 h 5 min, as a backstop. A time-limit kill is a final failure and is not retried, because
  three attempts could hold a slot for 9 hours. Only a lost lease is retried. `ROW_KILL_SECONDS` only
  runs when `each_level_in_separate_file` is on, so the supervisor passes
  `--logging.each_level_in_separate_file` and `--logging.file` to every child.
- **Lease renewal**: renewal matches on `lease_token` only, not on `lease_until`. Kill the job only if
  a renewal reaches Postgres and finds the token has changed. On a connection error, keep running, so
  a Postgres restart doesn't kill rows that are hours in. A VM cut off from Postgres may run a row
  another VM has taken. That wastes work, but the token blocks the old result.
- **Stolen lease**: every supervisor update matches the token. If the token has changed, kill the
  child and do nothing else, so the new attempt's state isn't overwritten.
- **Child exits before work starts**: `queue_feeder_db` sets `jobs.work_started_at` just before it
  yields the job. If the child exits with this unset, setup failed, for example from an expired
  session or bad config. Requeue the job with a 15-minute delay and give back its attempt. Rows stay
  queued rather than failing, because setup faults come from a config or VM, not the row.
- **Bad sheet config**: after 3 setup failures in a row on one sheet, set `sheets.paused_until` for 15
  minutes and raise one alert. While it stays broken, each further failure pauses it again with no new
  alert, so only one job per pause probes it. A job on that sheet getting past setup clears the count.
- **Child exits after work starts, with the job still `running`**: fail the job straight away rather
  than waiting for the lease to expire.
- **Repeated quick failures**: otherwise a broken VM fails jobs in seconds, claims more work than
  healthy VMs and empties the queue. After 3 in a row on one VM, across more than one sheet, the
  supervisor raises an alert and stops claiming for 15 minutes. It stays quiet if it pauses again, until
  a job finishes successfully. A quick failure is an exit before work starts, or a job that fails within
  60 seconds of starting.
- **Supervisor dies**: children get a parent-death signal, and a starting supervisor kills any
  leftovers from before.
- **Crawls**: killing `docker run` leaves the container crawling. Start it with
  `--name aa-job-<job id>` and run `docker rm -f` on kill. This also fixes `_kill_hung_row`. Also run it
  before starting, as a retry reuses the name.
- **Temp files**: `AA_SLOT_DIR` is set per slot and holds `logs/` and `tmp/`. `feed_item` creates its
  temp folder there, so the supervisor can wipe `tmp/` after a kill.
- **Daily reboot and deploys**: the reboot logic is in the cron scripts this replaces, so the
  supervisor takes it over. It stops claiming at `REBOOT_AT`, reboots when idle and forces it an hour
  later. Deploys use the same drain.

### Status flow

| Moment | Sheet status |
|---|---|
| Sync adds the job | `Queued` |
| Worker claims it | `Archive in progress` |
| Lease lost and job requeued | Stays `Archive in progress`, and the retry's claim writes nothing |
| Finished | Normal result, or `Archive failed ...` |
| Queued job cancelled | Cleared to blank |
| Client clears the status of a finished row | Treated as a new job, as today |

### Sync rules

Each cycle takes `snapshot_at` from the database before reading the sheet, so clocks can't disagree.
An entry is unwritten if both `written_at` and `discarded_at` are null.

1. Blank status, no active job, no unwritten outbox entry and no outbox entry for the row written at
   or after `snapshot_at`: add a job. The last check stops a result written during the read from
   being archived twice.
2. Blank status with an active job or an unwritten entry: do nothing. Without this rule, a finished
   job whose result hasn't reached the sheet would be archived twice.
3. `Queued` or `Archive in progress` with no active job, no unwritten entry and no outbox entry written
   at or after `snapshot_at`: raise an alert, at most once a day per row, because the sheet and queue
   disagree.
4. Any other status: do nothing.
5. A queued job whose row URL has changed or gone is cancelled, and an outbox entry clears `Queued`
   with `skip_url_check` set. The next cycle then adds the new URL.

Keep the existing valid URL and `must_have_folder_name_for_archive_to_run` checks from
`_process_rows`. A row that fails them gets a job with status `rejected` and an outbox entry for the
warning, so every sheet write has a job. A job and its first outbox entry share a transaction.

Read only the link, status and folder columns, not whole tabs, because cells can hold 50,000
characters.

Store the sheet's raw URL in `jobs.url`, not the sanitised one, as the writer compares against it.
`jobs.domain` comes from `domain_for_url` on the same raw URL.

### Sanity check before every write

The writer re-reads the row and compares the URL with `jobs.url`, unless `skip_url_check` is set, and
the status with `outbox.expected_status`. On a mismatch it writes nothing, sets `outbox.conflict` and
raises an alert. The results stay in Postgres, so nothing needs re-archiving. It does not search the
sheet for the URL. Updates for one job go out strictly in order, so later ones wait behind a conflict.

A small command resolves a conflict: retry clears `conflict`, and discard sets `discarded_at`. Without
it, rule 2 would block the row forever. A conflict is never rechecked by itself, even if the sheet looks
right again. If the row already shows the entry's status, the writer stopped before recording an
earlier write, so it marks the entry written rather than raising a conflict.

The writer finds tabs by gid, so results still arrive after a rename, even one that takes the tab off
the config's allow list. Sync connects to Google read-only, and only the writer connects read-write.

### Schema

```sql
CREATE TABLE sheets (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    spreadsheet_id text NOT NULL,
    worksheet_gid bigint NOT NULL,      -- survives a tab rename
    worksheet text NOT NULL,            -- current title, kept up to date by sync
    config text NOT NULL,
    weight int NOT NULL DEFAULT 1 CHECK (weight > 0),
    enabled bool NOT NULL DEFAULT false,
    setup_failures int NOT NULL DEFAULT 0,
    paused_until timestamptz,
    UNIQUE (spreadsheet_id, worksheet_gid)
);

CREATE TABLE jobs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sheet_id bigint NOT NULL REFERENCES sheets,
    row_number int NOT NULL,
    url text NOT NULL,
    domain text NOT NULL,
    folder text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'queued' CHECK (status IN
        ('queued', 'running', 'done', 'failed', 'cancelled', 'rejected')),
    attempts int NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    added_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,             -- first claim, so wait times include retries
    work_started_at timestamptz,        -- this attempt got past setup; cleared on each claim
    finished_at timestamptz,
    worker text,
    lease_token uuid,
    lease_until timestamptz,
    error text
);

-- Blocks a second active job for the same row
CREATE UNIQUE INDEX one_active_job ON jobs (sheet_id, row_number)
    WHERE status IN ('queued', 'running');

CREATE INDEX claimable ON jobs (id) WHERE status = 'queued';
CREATE INDEX running_by_sheet ON jobs (sheet_id) WHERE status = 'running';
CREATE INDEX jobs_by_row ON jobs (sheet_id, row_number);

CREATE TABLE outbox (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id bigint NOT NULL REFERENCES jobs,
    cells jsonb NOT NULL,
    expected_status text,
    skip_url_check bool NOT NULL DEFAULT false,
    conflict text,
    created_at timestamptz NOT NULL DEFAULT now(),
    written_at timestamptz,
    discarded_at timestamptz
);

CREATE INDEX outbox_by_job ON outbox (job_id);
CREATE INDEX unwritten ON outbox (id) WHERE written_at IS NULL AND discarded_at IS NULL;

-- What sync would have queued in shadow mode. Separate, so it can't collide with one_active_job
CREATE TABLE shadow_jobs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sheet_id bigint NOT NULL REFERENCES sheets,
    row_number int NOT NULL,
    url text NOT NULL,
    added_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz               -- the row stopped being blank, so cron picked it up
);

CREATE UNIQUE INDEX one_open_shadow_job ON shadow_jobs (sheet_id, row_number) WHERE closed_at IS NULL;

-- Folder ids shared between processes. See "Running rows at once"
CREATE TABLE drive_folders (
    parent_id text NOT NULL,
    name text NOT NULL,
    folder_id text NOT NULL,
    PRIMARY KEY (parent_id, name)
);

CREATE TABLE alerts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind text NOT NULL,
    message text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz
);

CREATE TABLE heartbeats (
    name text PRIMARY KEY,
    beat_at timestamptz NOT NULL DEFAULT now()
);
```

The full schema is in `src/auto_archiver/job_queue/migrations/`. `python -m auto_archiver.job_queue.migrate`
applies any new files in order and records them in `schema_migrations`. To change the schema, add the next
numbered file, and never edit one that's been applied. Each file's checksum is recorded, and the coordinator
won't start if any are unapplied, edited, or unknown to its code. It doesn't apply them itself: after pulling a
new migration, run `migrate`, then restart it.

### Claiming a job

The sheet with the fewest running jobs, relative to its weight, goes first, then the oldest job.

```sql
UPDATE jobs SET status = 'running', worker = $1, attempts = attempts + 1,
    started_at = COALESCE(started_at, now()), work_started_at = NULL,
    lease_token = gen_random_uuid(), lease_until = now() + interval '5 minutes'
WHERE id = (
    SELECT j.id FROM jobs j JOIN sheets s ON s.id = j.sheet_id
    WHERE j.status = 'queued' AND j.available_at <= now() AND s.enabled
        AND (s.paused_until IS NULL OR s.paused_until <= now())
    ORDER BY (SELECT count(*) FROM jobs r WHERE r.sheet_id = j.sheet_id
              AND r.status = 'running')::float / s.weight, j.id
    FOR UPDATE OF j SKIP LOCKED LIMIT 1
) RETURNING *, (SELECT config FROM sheets s WHERE s.id = jobs.sheet_id) AS config;
```

A worker can only save results if its `lease_token` still matches, so a worker that has been
replaced can't overwrite the newer result.

The first claim adds the `Archive in progress` outbox entry in the same transaction. A later claim of
the same job adds none. It checks for an existing entry rather than `attempts`, because a setup failure
gives its attempt back.

Fairness is per tab, not per client, so a client with 20 tabs gets 20 shares. Client fairness comes in
phase 3.

## Running rows at once

Drive folders can go wrong as soon as jobs overlap, so fix them in phase 1. The rest apply from 2
slots on one VM, so run 1 slot per VM until they're fixed, and fix them before rollout step 4.

| Problem | Where | Fix |
|---|---|---|
| Duplicate Drive folders | Folder ids are cached per process, and Drive search can lag by minutes (`gdrive_storage.py:83`) | Look up and create folders under an advisory lock, using `drive_folders` |
| Shared Chrome profile | The first browser locks `antibot_user_data`, and the fallback retries without the profile | Copy the profile per job |
| Shared cookie file | yt-dlp writes cookies back to the file on exit, so runs can overwrite each other | Copy cookies per job |
| OAuth token refresh | The token file is truncated then rewritten with no lock (`gdrive_storage.py:111`) | File lock, or a service account |
| Facebook crawl's shared files | `_fb_browsertrix_crawl` writes `url.txt` to the working directory and copies the profile to `~/aatmp` | A folder per slot |

## Known problems

1. **Duplicate uploads on retry**: accepted for phase 1. Uploads happen at the end of a row, except in
   `whisper_enricher`, so only a kill during that stage leaves files behind. Drive always creates a
   new file on retry. S3 and local overwrite if the key matches, and otherwise leave an orphan. The
   sheet links to the latest attempt, so these are harmless orphans. This assumes production doesn't
   use `random_no_duplicate`, which skips identical files. See "Questions for Dave".
2. **Shared logs**: `logs/1debug.log`, the other log files and `logs/hangs.log` have hard-coded paths.
   Use `logs/` under `AA_SLOT_DIR`, see "Supervisor rules". `aa-logs` must then read each slot's logs,
   and the `STARTED` and `DONE` lines must stay, because the timings use them.
3. **Postgres driver**: decided, see Decisions. `psycopg` is in `pyproject.toml`.
4. **Timeouts**: decided, see "Supervisor rules".
5. **"Archive in progress" relabelling**: `gsheet_feeder_db.py:92` assumes one server per worksheet. It
   stays for cron-owned sheets. Queue-owned sheets use leases instead. Cron must never run a config
   that covers a queue-owned tab, because it would relabel that tab's running rows.
6. **Sheets quota**: sync must not copy `_process_rows`, which makes an extra API read per blank row
   (`gsheet_feeder_db.py:89`). Use `batchGet` per spreadsheet, for the needed columns only, plus one
   metadata call for tabs. Writer volume is small, about 3 calls a minute at 3x.

## Questions for Dave

1. What are the Drive and S3 `path_generator` and `filename_generator` settings in production, and is
   `random_no_duplicate` on?
2. Does Drive use OAuth or a service account?
3. Does every VM hold the same secrets? A shared queue runs any sheet on any VM, so a config or cookie
   file on only one VM would fail. If accounts differ per VM, moving a sheet also changes its account,
   and `sheets` needs a nullable `workers` column to limit where a sheet runs. Either way, the
   coordinator also needs the configs and service accounts, and changes must reach every VM.

## Rollout

1. **Shadow mode**: the sync records what it would queue in `shadow_jobs` and changes nothing. It
   connects to Google with read-only scopes, so it can't write to a sheet. `register` adds a config's
   tabs, switched off. `closed_at - added_at` gives the real wait for each row under cron, to within a
   cycle. A row cron picks up between two cycles is never seen, so short waits are undercounted.
2. **Small sheet**: move one small spreadsheet, with all its tabs, because a cron config covers a whole
   spreadsheet. `sheets.enabled` is the switch for whether cron or the queue owns a sheet. Never both.
3. **Busy sheets**: move larger ones in turn, keeping total slots at 5 until stable.
4. **Scale up**: first fix the problems in "Running rows at once". Then raise slots gradually (2, 3, 4,
   6, 8 per host) on live work, watching disk latency, account errors and Drive
   `userRateLimitExceeded` warnings. Stop when time per domain or those warnings rise, or memory or
   disk runs short. This gives the first real capacity figure. Phase 1 adds no
   capacity until this step: at 5 slots the simulation gives a 3.9 h 95th percentile at 3x, so do it
   before volume triples.

**Moving a spreadsheet** (`python -m auto_archiver.job_queue.sync ...`):

1. Coordinator and a supervisor running, with the sheet in shadow mode showing the expected rows.
2. Turn the spreadsheet's cron line off, and wait for the cron run to finish.
3. Check no row still says `Archive in progress`. A killed cron run can leave one, and only the old
   feeder relabels those. Blank it for a rerun, or set it to a final status.
4. `enable <spreadsheet_id>`. All its tabs move, and tabs added later start on.

**Rollback**: `rollback <spreadsheet_id>` turns the spreadsheet off, cancels its waiting jobs and clears
their status. It reports what's still running or unwritten, so run it again until it says cron can go
back on. Cron must not start sooner, or it relabels running rows as `Problem in archive`. The clear step
matters because the old feeder treats any non-blank status as finished. A conflict counts as unwritten
until someone retries or discards it.

Service files are in `scripts/job_queue/`.

## Later phases

Rough estimates. Each runs alongside the existing setup.

| Phase | Work | Result | Time |
|---|---|---|---|
| 1 | This spec | Bursts spread across VMs, no stuck rows, real wait times | 2 to 3 weeks |
| 2 | S3 staging, separate tasks for Drive, OpenTimestamps and Wayback, circuit breakers | About 45% less time per row on the workers, outages stop delaying rows | 2 to 3 weeks |
| 3 | Separate capture and browser pools, first results sooner, client fairness | Much faster first results | 3 to 4 weeks |
| 4 | Account pool and per-domain budgets if wanted, browser limits based on disk latency and RAM | A safer route to 10x | 2 to 3 weeks |
| 5 | API intake, automatic recovery actions | Less hands-on work | When needed |

The 45% in phase 2 needs measuring. The step table adds up to 238 s against an average row of about
292 s, so 54 s is unaccounted for, and the saving is 41 to 50% depending on which is used.

Proposed targets, to be checked against the baseline from phase 1:

| Measure | Target |
|---|---|
| First capture visible (from phase 3) | 95% within 5 minutes |
| Full evidence complete | 95% within 1 hour, excluding third-party outages |
| Stuck rows | 0 |
| Human attention | Daily digest only |

Image rating later becomes a separate low-priority task. At about 170 s per image, CPU manages about
508 images a day, so it needs file selection, a "not yet rated" status and possibly a GPU.

## Working rules

- Follow `CLAUDE.md` in this directory and the global rules. Don't run the app or live tests, and
  never use real cookies or sessions.
- Tests run with `poetry run pytest -m "not download"`.
- No real sheet names, URLs or handles in code, tests or docs. Use placeholders.
- Ask before adding a dependency.

## Next steps

1. Answer "Questions for Dave".
2. Build in this order, with tests at each step:
   1. Done: the schema and `db.py`, with claim, lease and race tests against a separate local test
      database. These tests skip if there is no test database.
   2. Done: sync in shadow mode, which can run in production early, tested on a real sheet.
   3. Done, tested on a real sheet: the pure cell builder, split out of `done()`, the writer, and live
      sync, which applies the same rules to `jobs` and `outbox` on tabs with `enabled` on.
   4. Done, normal run, kill and drain tested on a real sheet: `queue_feeder_db`, the supervisor with the failure rules,
      and the coordinator. Not in this step: sending alert emails, a service file for each VM (use
      `KillMode=mixed`, so stopping it drains rather than kills the children), and the fixes in
      "Running rows at once".
   5. Move one small spreadsheet.

Module layout:

```
src/auto_archiver/job_queue/   # not "queue": python src/auto_archiver would shadow the standard library
    migrations/      # numbered SQL files, applied by migrate.py
    migrate.py
    db.py            # connection and all SQL
    sync.py          # includes the pure rules function
    writer.py
    coordinator.py   # runs sync, writer and cleanup
    worker_supervisor.py
src/auto_archiver/modules/queue_feeder_db/
```
