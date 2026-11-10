-- What a running job's archiver is doing now, eg 'wacz_extractor_enricher', and since when that step started
ALTER TABLE jobs ADD COLUMN stage text, ADD COLUMN stage_at timestamptz;

-- The lowest memory available and disk free on its worker while the job ran, over all its attempts
ALTER TABLE jobs ADD COLUMN min_mem_available_bytes bigint, ADD COLUMN min_disk_free_bytes bigint;

-- One row per step of each attempt, eg an extractor, an enricher, 'storing', for how long each took.
-- ended_at stays empty for the step an attempt was in when it was killed or crashed
CREATE TABLE job_stages (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id bigint NOT NULL REFERENCES jobs,
    attempt int NOT NULL,
    step text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz
);

CREATE INDEX job_stages_by_job ON job_stages (job_id);
