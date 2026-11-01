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

Placement:

- `pve2`: Postgres, sync, sheet writer and monitor, because these need the RAID.
- `pve3`: browser-heavy workers, using its SSD as disposable scratch space. Never Postgres.
- Glimmer image rating may also run on `pve3` later, at low priority. See
  `/home/dave/code/hackathon/image-spec.md`.
- Everything stays on these two machines. No cloud burst workers.

## Decisions

- Postgres is the queue, using `SKIP LOCKED`. No broker. Even 10x volume is well under one task a
  second.
- Python throughout, because workers call the existing Python modules.
- Stay on Python 3.12 for now. `pyproject.toml` needs `>=3.10,<3.13`, and an upgrade is its own
  change later.
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

- **Sheet sync**: one service reads each enabled sheet every minute or two and adds jobs. It replaces
  the per-sheet polling in the cron scripts.
- **Supervisor**: one per worker VM, with N slots. Each slot claims a job and runs
  `auto_archiver --config <config> --job <id>` as a child process. It renews the lease every minute,
  enforces the time limit and kills the process tree.
- **`queue_feeder_db`**: a new module, both Feeder and Database, modelled on `gsheet_feeder_db`. It
  yields the one claimed job and writes results to Postgres. Archiving no longer touches Sheets, so
  a Sheets outage can't fail a row.
- **Sheet writer**: batches results back to the sheets. It is the only part that writes to them.
- **Cleanup**: requeues jobs whose lease expired, with a delay, and fails them after 3 attempts.

The orchestrator barely changes. Nothing outside `gsheet_feeder_db` reads the sheet row context, and
the orchestrator already gets work from a feeder and reports through database hooks. Reuse the cell
building in `GsheetsFeederDB.done()` by extracting it so it can produce a list of cell updates.

### Status flow

| Moment | Sheet status |
|---|---|
| Sync adds the job | `Queued` |
| Worker claims it | `Archive in progress` |
| Finished | Normal result, or `Archive failed ...` |
| Client clears the status of a finished row | Treated as a new job, as today |

### Sync rules

1. Blank status, no active job and no unwritten outbox entry: add a job.
2. Blank status with an active job or an unwritten entry: do nothing. Without this rule, a finished
   job whose result hasn't reached the sheet would be archived twice.
3. `Queued` or `Archive in progress` with no active job and no unwritten entry: raise an alert,
   because the sheet and queue disagree.
4. Any other status: do nothing.
5. A queued job whose row URL has changed or gone is cancelled.

Keep the existing valid URL and `must_have_folder_name_for_archive_to_run` checks from
`_process_rows`.

### Sanity check before every write

The writer re-reads the row and compares the URL with `jobs.url` and the status with
`outbox.expected_status`. On a mismatch it writes nothing, sets `outbox.conflict` and raises an
alert. The results stay in Postgres, so nothing needs re-archiving. It does not search the sheet for
the URL. Updates for one job go out strictly in order.

### Schema

```sql
CREATE TABLE sheets (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    spreadsheet_id text NOT NULL,
    worksheet text NOT NULL,
    config text NOT NULL,
    weight int NOT NULL DEFAULT 1,
    enabled bool NOT NULL DEFAULT false,
    UNIQUE (spreadsheet_id, worksheet)
);

CREATE TABLE jobs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sheet_id bigint NOT NULL REFERENCES sheets,
    row_number int NOT NULL,
    url text NOT NULL,
    domain text NOT NULL,
    status text NOT NULL DEFAULT 'queued',
    attempts int NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    added_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
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

CREATE TABLE outbox (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id bigint NOT NULL REFERENCES jobs,
    cells jsonb NOT NULL,
    expected_status text,
    conflict text,
    created_at timestamptz NOT NULL DEFAULT now(),
    written_at timestamptz
);
```

### Claiming a job

The sheet with the fewest running jobs, relative to its weight, goes first, then the oldest job.

```sql
UPDATE jobs SET status = 'running', worker = $1, attempts = attempts + 1,
    started_at = now(), lease_token = gen_random_uuid(),
    lease_until = now() + interval '5 minutes'
WHERE id = (
    SELECT j.id FROM jobs j JOIN sheets s ON s.id = j.sheet_id
    WHERE j.status = 'queued' AND j.available_at <= now() AND s.enabled
    ORDER BY (SELECT count(*) FROM jobs r WHERE r.sheet_id = j.sheet_id
              AND r.status = 'running')::float / s.weight, j.id
    FOR UPDATE OF j SKIP LOCKED LIMIT 1
) RETURNING *;
```

A worker can only save results if its `lease_token` still matches, so a worker that has been
replaced can't overwrite the newer result.

## Known problems to solve

1. **Duplicate uploads on retry**: a job killed halfway may have already uploaded files. Read the
   storage modules to see how each one names files. Not checked yet.
2. **Shared logs**: `logs/1debug.log`, the other log files and `logs/hangs.log` have hard-coded paths.
   They must be per job or per slot once a VM runs more than one slot.
3. **Postgres driver**: `psycopg` is not in `pyproject.toml`. Adding it needs Dave's approval.
4. **Timeouts**: the orchestrator already has a kill timer (`ROW_KILL_SECONDS`) and hang dumps. Decide
   how these fit with the supervisor's process-level limit.
5. **"Archive in progress" relabelling**: `gsheet_feeder_db.py:92` assumes one server per worksheet. It
   stays for cron-owned sheets. Queue-owned sheets use leases instead.
6. **Sheets quota**: confirm sync and writer calls stay within Google's limits, using batched reads.

## Rollout

1. **Shadow mode**: the sync records what it would queue and changes nothing. This also gives real
   `added_at` times.
2. **Small sheet**: move one small sheet. `sheets.enabled` is the switch for whether cron or the queue
   owns a sheet. Never both.
3. **Busy sheets**: move larger ones in turn, keeping total slots at 5 until stable.
4. **Scale up**: raise slots gradually (2, 3, 4, 6, 8 per host) on live work, watching disk latency
   and account errors. Stop when time per domain rises or memory or disk runs short. This gives the
   first real capacity figure.

**Rollback**: one command that turns a sheet off, cancels its waiting jobs and clears `Queued` from
those rows, then turn its cron line back on. Running jobs are left to finish. The clear step matters
because the old feeder treats any non-blank status as finished.

## Later phases

Rough estimates. Each runs alongside the existing setup.

| Phase | Work | Result | Time |
|---|---|---|---|
| 1 | This spec | Bursts spread across VMs, no stuck rows, real wait times | 2 to 3 weeks |
| 2 | S3 staging, separate tasks for Drive, OpenTimestamps and Wayback, circuit breakers | About 45% less time per row on the workers, outages stop delaying rows | 2 to 3 weeks |
| 3 | Separate capture and browser pools, first results sooner, client fairness | Much faster first results | 3 to 4 weeks |
| 4 | Account pool and per-domain budgets if wanted, browser limits based on disk latency and RAM | A safer route to 10x | 2 to 3 weeks |
| 5 | API intake, automatic recovery actions | Less hands-on work | When needed |

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
- This directory is not a git repository at the time of writing.
- Tests run with `poetry run pytest -m "not download"`.
- No real sheet names, URLs or handles in code, tests or docs. Use placeholders.
- Ask before adding a dependency.

## Next steps

1. Read the storage modules to answer the duplicate upload question.
2. Agree the module layout and the small changes needed in the orchestrator.
3. Add the schema as a raw SQL file, and pick the database name and connection config.
4. Build the sync in shadow mode first, with tests.
