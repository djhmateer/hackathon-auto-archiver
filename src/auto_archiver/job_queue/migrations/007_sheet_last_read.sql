-- What sync last found in each tab, for the dashboard. Only tabs that are on are read
ALTER TABLE sheets
    ADD COLUMN last_read_at timestamptz,
    ADD COLUMN url_rows int,            -- rows with a URL
    ADD COLUMN blank_rows int;          -- of those, rows with a blank status
