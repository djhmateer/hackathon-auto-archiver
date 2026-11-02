-- Postgres job queue for the archiver. See queue-spec.md

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

-- Folder ids shared between processes, as Drive search can lag by minutes
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
