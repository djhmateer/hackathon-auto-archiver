# Hackathon Auto Archiver

This is a fork of [Bellingcat's Auto Archiver](https://github.com/bellingcat/auto-archiver), extended for a hackathon. It adds:

1. **A Postgres job queue.** Archiving work is shared across a pool of worker VMs instead of each spreadsheet being tied to one machine.
2. **A distressing image analyser.** A local vision LLM rates every archived image and video 1–5 for how distressing it is, and blurs anything rated 3 or above in the archive.

Both features are new work and are not yet in production.

## Background

I work in human rights digital archiving and monitoring with charitable investigative organisations, mostly in the UK, in human rights and law. I have run Bellingcat's open source Auto Archiver for the past five years through [my fork](https://github.com/djhmateer/auto-archiver), starting with [OSR4Rights Tools](https://osr4rightstools.org/).

In 2024 the work won the ESRC Celebrating Impact Prize for [research that is helping the UN and courts around the world to assess mobile phone evidence in war crimes prosecutions and other human rights cases](https://www.swansea.ac.uk/press-office/news-events/news/2024/11/research-on-using-mobile-phone-evidence-in-human-rights-cases-wins-prestigious-impact-prize.php).

Over the last year the archiver has processed more than 60,000 URLs on five VMs, running on a Proxmox hypervisor on my own hardware. Each spreadsheet runs on its own VM. As a result, investigators often wait for one busy VM to finish while the others sit idle.

## Feature 1: Postgres job queue

A coordinator service and a Postgres database run on their own VM. Each worker VM runs a supervisor that claims jobs from Postgres, one at a time, and runs the existing archiver on each job.

```
 Google Sheets  <──read──  Coordinator  ──write──>  Google Sheets
                               │  (sync, sheet writer, cleanup)
                               ▼
                           Postgres  (jobs, sheet_writes, admin_alerts, ...)
                               ▲
          ┌────────────────────┼────────────────────┐
   Worker supervisor    Worker supervisor    Worker supervisor
   (one job per VM)     (one job per VM)     (one job per VM)
          │
          └─> archiver child process (queue_feeder_db feeder/database)
```

- **Load balancing.** Any worker can run any sheet's rows. Sheets take turns, weighted by `sheets.weight`.
- **Politeness.** Only one job per site (e.g. `vimeo.com`) runs at a time across the whole pool.
- **Resilience.** Workers hold a 5-minute lease that they renew. A job whose worker dies is requeued, and fails only after 3 lost workers. If the archiver fails during setup, the attempt is not counted, and a sheet with a broken config is paused.
- **Sheet safety.** Only the sheet writer writes to a sheet. Before each write it re-reads the row and records a conflict instead of overwriting a change made by someone else.
- **Archive stats.** Every finished job records its extractor, timings and each stored file's size, type and hash. These are kept for reporting.

Code: `src/auto_archiver/job_queue/`, `src/auto_archiver/modules/queue_feeder_db/` and `scripts/job_queue/`.

The design is in [queue-spec.md](queue-spec.md). Setup, deployment and operations are in [README_QUEUE.md](README_QUEUE.md).

## Feature 2: Distressing image analyser

Investigators often have to review distressing imagery and video. They usually rate it by hand on a 1–5 scale, so that colleagues don't have to see it unless they need to. Developers working on the archive face the same problem. The `graphic_content_llm_enricher` warns people before they see such content.

- Each image, and each video's first frame, is sent to a vision model running locally on [Ollama](https://ollama.com/). Nothing leaves the network.
- The model returns a rating and a one-line description:

  | Rating | Meaning |
  |---|---|
  | 1 | No graphic content |
  | 2 | Upsetting context only, nothing graphic visible |
  | 3 | Death or injury is clear, but the body is hidden |
  | 4 | Injury or a body is visible, but limited |
  | 5 | Clear images of human bodies |

- The HTML archive page blurs anything rated 3 or more until it is clicked. It also blurs anything that couldn't be rated (`needs review`), and hides those items' descriptions.
- The highest rating and a short summary are written to the spreadsheet's `highest graphic rating` and `images summary` columns. Descriptions of items rated 3 or more never go into the summary, because a sheet cannot be blurred.
- Screenshots, small images (under 200 px), duplicates and audio-only files are skipped. A time limit, 20 minutes by default, keeps long posts from stalling a worker.

The default model is `muse-glimmer:30b-q4_K_M`. It gives good results, but on a CPU-only machine each image takes a few minutes.

To enable it, add the enricher to the orchestration config:

```yaml
steps:
  enrichers:
    - graphic_content_llm_enricher
    # ... other enrichers

graphic_content_llm_enricher:
  ollama_url: http://<ollama_host>:11434
  model: muse-glimmer:30b-q4_K_M
  max_minutes: 20
```

All the options are listed in `src/auto_archiver/modules/graphic_content_llm_enricher/__manifest__.py`.

## Getting started

Requirements: Python 3.11 or 3.12, [Poetry](https://python-poetry.org/), and Postgres for the queue.

```bash
poetry install
poetry run auto-archiver --config secrets/orchestration.yaml
```

Configuration files and credentials live in `secrets/`, which is not in git. For general installation and configuration, see the [upstream documentation](https://auto-archiver.readthedocs.io/en/latest/).

## Tests

```bash
poetry run pytest -m "not download"
```

The job queue tests run against a real Postgres database, which must be named `*_test`, because the tests wipe it. Set `AA_TEST_DATABASE_URL`, or create the default: `postgresql://bob:password@localhost:5432/auto_archiver_test`. If no test database is found, those tests are skipped.

The `download` tests reach live sites, so they are excluded.

## About this fork

This repository is a snapshot of my production fork, taken on 31 October 2026 at version 1.2.9, with the hackathon work on top. Compared with upstream, the production fork also has:

- Uwazi integration
- Enhanced Facebook crawling
- Overwriting of spreadsheet cells
- Image size filtering in WACZ files
- A fixed `metadata.json` filename
- Fixed log file locations under `logs/`
- Automatic updates disabled

## Acknowledgements and contact

Auto Archiver is developed by [Bellingcat](https://www.bellingcat.com/). Read their [article about the tool](https://www.bellingcat.com/resources/2022/09/22/preserve-vital-online-content-with-bellingcats-auto-archiver-tool/).

Thanks to Meta for the hackathon, which prompted both features.

If you work in digital archiving, monitoring or human rights and would like to talk, contact dave@hmsoftware.co.uk or find me on the hackathon Discord.

## Licence

MIT. See [LICENSE](LICENSE).
