# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Auto Archiver is a Python tool for automatically archiving web content (social media posts, videos, images, webpages) in a secure and verifiable way. This is a fork of the original Bellingcat Auto Archiver with additional features including Uwazi integration, Facebook crawling, and various customizations.

## Architecture

The project follows a modular pipeline architecture:

1. **Feeders** - Input sources (CSV, Google Sheets, CLI, etc.) located in `src/auto_archiver/modules/`
2. **Extractors** - Content extractors for different platforms (Instagram, Twitter, Telegram, etc.)
3. **Enrichers** - Add metadata, hashes, timestamps to archived content
4. **Databases** - Track archiving status (CSV, Console, API, Google Sheets)
5. **Storages** - Save content (Local, S3, Google Drive)
6. **Formatters** - Format output (HTML, JSON)

Configuration is done via YAML files typically stored in `secrets/` directory.


## Key Files and Directories

- `src/auto_archiver/` - Main source code
  - `core/orchestrator.py` - Main orchestration logic
  - `modules/` - All feeder, extractor, enricher, database, storage, and formatter modules
  - `utils/` - Utility functions
  - `uwazi_api/` - Uwazi integration
- `secrets/` - Configuration files and credentials (not in git)
  - `orchestration*.yaml` - Configuration files for different archiving workflows
  - `service_account*.json` - Google service account credentials
  - `*.session` - Telegram session files
  - `*_cookies.txt` - Cookie files for authentication
- `tests/` - Test suite
- `pyproject.toml` - Project dependencies and configuration

## Module Development

New modules should follow the existing pattern:
1. Create a directory in `src/auto_archiver/modules/`
2. Include `__manifest__.py` with module metadata
3. Implement the appropriate base class (Feeder, Extractor, Enricher, etc.)
4. Add configuration handling

## Connecting to Servers

- The only hosts you may SSH to are the `pve3` VMs, all on `192.168.1.212` by port:
  - `ssh 192.168.1.212 -p 301` - LLM
  - `ssh 192.168.1.212 -p 340` - coordinator (Postgres job queue)
  - `ssh 192.168.1.212 -p 341` - glanworker1 (was worker41)
  - `ssh 192.168.1.212 -p 342` - glanworker2 (was worker42)
  - `ssh 192.168.1.212 -p 343` - hmsworker1 (was testworker1)
  - `ssh 192.168.1.212 -p 344` - hmsworker2
  - `ssh 192.168.1.212 -p 345` - cirworker1
  - `ssh 192.168.1.212 -p 346` - cirworker2
- Never connect to any other production server or host. The old cron servers 11-15 are retired (Nov 2026).
- On the pve3 VMs, read-only commands are fine (logs, `journalctl`, `df`, `SELECT`s). Anything that changes a VM
  (restarts, edits, installs, `INSERT`/`UPDATE`/`DELETE`) needs the user's go-ahead in that message; otherwise give
  them the exact command. Never read anything in a VM's `secrets/` or `.env`.

## Investigating Production Errors

Production is the Postgres job queue on pve3 (see README_QUEUE.md): the coordinator syncs the sheets and writes
results back, and the two workers each run one archiver job at a time. Server times are UTC; compare in UTC.

Where to look, in this order:
1. The queue database on the coordinator - every job, its attempts, worker, timings, error, sheet writes, files
   stored, alerts and heartbeats. Query it read-only over ssh:
   `ssh 192.168.1.212 -p 340 "PGPASSWORD=password psql -h localhost -U bob -d auto_archiver -c \"SELECT ...\""`
   - `jobs` (`id`, `sheet_id`, `row_number`, `domain`, `status`, `attempts`, `worker`, `started_at`,
     `finished_at`, `error`, `git_commit`, `extractor`, ...), `sheets`, `sheet_writes` (`cells` JSON, eg the status
     written), `job_files`, `admin_alerts`, `heartbeats`. The schema is in `src/auto_archiver/job_queue/migrations/`.
2. The coordinator's log: `journalctl -u aa-coordinator` on 340 - each job queued, claimed and finished, workers
   going quiet, sheet writes, sync and alert email errors.
3. A worker's logs, on 341 to 346 (from `jobs.worker`), in `~/auto-archiver/logs/`:
   - `1debug.log` (and rotated copies) - the archiver at DEBUG. Every line has the job id after the level, eg
     `| INFO     | job 92 | auto_archiver.core.orchestrator:feed:123 - ...`, so `grep "| job 92 |"` gives a whole job
   - `console/job_<id>.log` - the archiver's console output at ERROR, one header line per attempt: setup errors
     (before logging starts) and crashes. Kept 14 days
   - `hangs.log` - every thread's stack when a row runs over 30 minutes, or on `kill -USR1 <pid>`
   - `journalctl -u aa-worker-supervisor` - claims, finishes, time-limit kills, reboots; `journalctl -k` for OOM
     kills
4. `aa_logs_pve3` on localhost, once the `aa-logs-pve3` project exists - everything above imported daily by the
   user. Check its freshness (max timestamp per server) and ask the user to import rather than running it yourself.

The DEBUG-level detail around a WARNING/ERROR line (surrounding requests, timings, retries) often gives much better
context for root-causing an issue than the bare error line alone - read a job's surrounding lines rather than
relying only on what was pasted into the conversation.

The `aa_logs` database and `/mnt/t/aa-dashboard-log-import/` only hold the retired servers 11-15's history (up to
Nov 2026). Use them only for comparisons with how the cron servers behaved.

## Testing

- Run tests with `poetry run pytest -m "not download"`.
- Never run the live tests (`-m download`, or plain `pytest`, which includes them), and don't offer to run them.
- Never run anything by hand (yt-dlp, browsertrix-crawler, curl, scripts) that loads cookies or sessions from
  `secrets/` (`*_cookies.txt`, `profile.tar.gz`, `*.session`) against real sites.
- Why: sending real cookies outside production can trip bot detectors and get the accounts banned.
- Never run anything against a real site (browsertrix-crawler, yt-dlp, curl, scripts, a docker crawl), even
  without cookies, unless the user explicitly says to run it. Agreeing to "try" an approach is not permission
  to run it: propose the exact command and wait for a go-ahead.
- Never run the project's code yourself outside `poetry run pytest -m "not download"` unless the user explicitly
  says to. That includes the app and ad-hoc scripts that import its modules (e.g. calling an extractor or
  enricher on a downloaded WACZ to check a fix), even offline. Reading data with plain tools (warcio, grep) to
  investigate is fine; to check a fix against real data, propose the exact command and wait for a go-ahead.

## Version Control

- Never run `git commit` or `git push` (or any equivalent, e.g. via the `cp` skill). The user handles all commits and pushes to the remote manually.

## Important Customizations in This Fork

- Uwazi integration for content management
- Enhanced Facebook crawling capabilities
- Spreadsheet cell overwriting allowed
- Hardcoded log file locations (`logs/1debug.log`, etc.)
- WACZ image size filtering
- Fixed metadata.json filename for automation
- Updates disabled to prevent production issues