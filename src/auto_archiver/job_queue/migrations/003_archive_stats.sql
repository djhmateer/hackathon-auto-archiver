-- What each archive produced, for a dashboard. Saved by the queue feeder when a job finishes

-- who the sheet is for, set by hand for now, so storage can be totalled per client
ALTER TABLE sheets ADD COLUMN client text NOT NULL DEFAULT '';

ALTER TABLE jobs
    ADD COLUMN git_commit text,         -- the worker's code, saved at each claim
    ADD COLUMN title text,
    ADD COLUMN archive_url text,
    ADD COLUMN extractor text,          -- the one that succeeded, eg 'vimeo' from 'vimeo: success'
    ADD COLUMN posted_at timestamptz,   -- when the original was posted, if the extractor found it
    ADD COLUMN cached bool,
    ADD COLUMN store_failures int;      -- files that couldn't be uploaded

-- One row per file stored, for each attempt that finished, so a retry's files are counted too
CREATE TABLE job_files (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id bigint NOT NULL REFERENCES jobs,
    attempt int NOT NULL,
    role text NOT NULL DEFAULT '',      -- the media's id, eg 'browsertrix', 'metadata_json', '' for the main media
    key text,                           -- its storage key, eg 'aadt198/6d85a9a134487bc6ba0289ac.mp4'
    url text,
    mimetype text,
    bytes bigint,
    hash text,                          -- eg 'SHA-256:...'
    duration_seconds numeric,           -- for video and audio, if the extractor found it
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX job_files_by_job ON job_files (job_id);
