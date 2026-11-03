-- The spreadsheet's name, eg 'AA Dave Testing', kept up to date by sync so the logs can show it
ALTER TABLE sheets ADD COLUMN spreadsheet_name text NOT NULL DEFAULT '';
