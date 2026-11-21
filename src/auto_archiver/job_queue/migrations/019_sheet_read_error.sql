-- Why sync couldn't read a tab, eg a missing url or status column, for the dashboard. Cleared by the next good read
ALTER TABLE sheets ADD COLUMN read_error text;
