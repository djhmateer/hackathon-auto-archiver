-- When sync read each tab, for the dashboard's slowest gap between reads. Only the last hour is kept
-- (db.save_tab_read deletes a tab's older reads), and a tab's reads go with it
CREATE TABLE sheet_reads (
    sheet_id bigint NOT NULL REFERENCES sheets ON DELETE CASCADE,
    read_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sheet_id, read_at)
);
